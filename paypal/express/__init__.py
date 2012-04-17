import urllib
import urlparse
import time
import logging
import requests
from decimal import Decimal as D
from django.conf import settings
from oscar.apps.payment.exceptions import PaymentError
from paypal.express import models

# PayPal methods
SET_EXPRESS_CHECKOUT = 'SetExpressCheckout'
GET_EXPRESS_CHECKOUT = 'GetExpressCheckoutDetails'
DO_EXPRESS_CHECKOUT = 'DoExpressCheckoutPayment'

SALE, AUTHORIZATION, ORDER = 'Sale', 'Authorization', 'Order'
API_VERSION = getattr(settings, 'PAYPAL_API_VERSION', '60.0')

# Anonymous checkout must be abled
if not settings.OSCAR_ALLOW_ANON_CHECKOUT:
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured("OSCAR_ALLOW_ANON_CHECKOUT must be True for PayPal Express to work")

logger = logging.getLogger('paypal.express')


class PayPalError(PaymentError):
    pass


def _fetch_response(method, extra_params):
    """
    Fetch the response from PayPal and return a transaction object
    """
    # Build parameter string
    params = {
        'METHOD': method,
        'VERSION': API_VERSION,
        'USER': settings.PAYPAL_API_USERNAME,
        'PWD': settings.PAYPAL_API_PASSWORD,
        'SIGNATURE': settings.PAYPAL_API_SIGNATURE,
    }
    params.update(extra_params)
    payload = urllib.urlencode(params.items())

    # Make request
    logger.debug("Making request: %s" % payload)
    start_time = time.time()
    if getattr(settings, 'PAYPAL_SANDBOX_MODE', True):
        url = 'https://api-3t.sandbox.paypal.com/nvp'
    else:
        url = 'https://www.paypal.com/nvp'
    response = requests.post(url, payload)
    if response.status_code != 200:
        logger.error("Received status code %s from PayPal",
                     response.status_code)
        raise PayPalError("Unable to communicate with PayPal")

    response_time = (time.time() - start_time) * 1000.0
    response_dict = urlparse.parse_qs(response.content)
    logger.debug("Received response: %s" % response.content)

    # Record transaction data - we save this model whether the txn
    # was successful or not
    txn = models.Transaction(
        method=method,
        version=API_VERSION,
        ack=response_dict['ACK'][0],
        raw_request=payload,
        raw_response=response.content,
        response_time=response_time,
    )
    if txn.is_successful:
        txn.correlation_id = response_dict['CORRELATIONID'][0]
        if method == SET_EXPRESS_CHECKOUT:
            txn.amount = params['AMT']
            txn.currency = params['CURRENCYCODE']
            txn.token = response_dict['TOKEN'][0]
        elif method == GET_EXPRESS_CHECKOUT:
            txn.token = params['TOKEN']
            txn.amount = D(response_dict['AMT'][0])
            txn.currency = response_dict['CURRENCYCODE'][0]
        elif method == DO_EXPRESS_CHECKOUT:
            txn.token = params['TOKEN']
            txn.amount = params['AMT']
            txn.currency = response_dict['CURRENCYCODE'][0]
    else:
        if 'L_ERRORCODE0' in response_dict:
            txn.error_code = response_dict['L_ERRORCODE0'][0]
        if 'L_LONGMESSAGE0' in response_dict:
            txn.error_message = response_dict['L_LONGMESSAGE0'][0]
    txn.save()

    if not txn.is_successful:
        raise PayPalError("Error %s - %s" % (txn.error_code, txn.error_message))

    return txn


def set_txn(basket, currency, return_url, cancel_url, action=SALE):
    """
    Register the transaction with PayPal to get a token which we use in the
    redirect URL.  This is the 'SetExpressCheckout' from their documentation.
    """
    # PayPal have an upper limit on transactions.  It's in dollars which is 
    # a fiddly to work with.  Lazy solution - only check when dollars are used as
    # the PayPal currency.
    amount = basket.total_incl_tax
    if currency == 'USD' and amount > 10000:
        raise PayPalError('PayPal can only be used for orders up to 10000 USD')

    params = {
        'AMT': amount,
        'CURRENCYCODE': currency,
        'RETURNURL': return_url,
        'CANCELURL': cancel_url,
        'PAYMENTACTION': action,
    }

    # Add item details
    for index, line in enumerate(basket.all_lines()):
        product = line.product
        params['L_NAME%d' % index] = product.get_title()
        params['L_NUMBER%d' % index] = product.upc
        params['L_DESC%d' % index] = product.description
        params['L_AMT%d' % index] = line.unit_price_incl_tax
        params['L_QTY%d' % index] = line.quantity

    # We include tax in the prices rather than separately as that's how it's
    # done on most British/Australian sites.  Will need to refactor in the
    # future no doubt
    params['ITEMAMT'] = basket.total_incl_tax
    params['TAXAMT'] = D('0.00')

    params['SHIPPINGAMT'] = D('0.00')
    params['HANDLINGAMT'] = D('0.00')

    txn = _fetch_response(SET_EXPRESS_CHECKOUT, params)

    # Construct return URL
    params = (('cmd', '_express-checkout'),
              ('token', txn.token),
              ('AMT', amount),
              ('CURRENCYCODE', currency),
              ('RETURNURL', return_url),
              ('CANCELURL', cancel_url))
    return '%s?%s' % (settings.PAYPAL_EXPRESS_URL, urllib.urlencode(params))


def get_txn(token):
    """
    Fetch details of a transaction from PayPal using the token as
    an identifier.
    """
    return _fetch_response(GET_EXPRESS_CHECKOUT, {'TOKEN': token})


def do_txn(payer_id, token, amount, currency, action=SALE):
    """
    DoExpressCheckoutPayment
    """
    params = {
        'PAYERID': payer_id,
        'TOKEN': token,
        'AMT': amount,
        'CURRENCYCODE': currency,
        'PAYMENTACTION': action,
    }
    return _fetch_response(DO_EXPRESS_CHECKOUT, params)