# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import random
import uuid
from datetime import datetime

from openfeature import api
from opentelemetry import baggage, metrics, trace
from opentelemetry.trace import Status, StatusCode

from payment.card_validator import get_card_details
from payment.logger import error_type, get_logger

LOYALTY_LEVEL = ["platinum", "gold", "silver", "bronze"]

logger = get_logger()
tracer = trace.get_tracer("payment")
meter = metrics.get_meter("payment")
transactions_counter = meter.create_counter("demo.payment.transactions", unit="{transaction}")


class ChargeError(Exception):
    pass


def charge(request, flag_client=None, now=None):
    flag_client = flag_client or api.get_client()
    span = tracer.start_span("charge")
    try:
        return _charge(span, request, flag_client, now or datetime.now())
    except BaseException as err:
        span.record_exception(err, attributes={"exception.type": error_type(err)})
        span.set_status(Status(StatusCode.ERROR, str(err)))
        span.set_attribute("error.type", error_type(err))
        raise
    finally:
        span.end()


def _charge(span, request, flag_client, now):
    synthetic_request = baggage.get_baggage("synthetic_request") == "true"
    if synthetic_request:
        span.set_attribute("user_agent.synthetic.type", "test")

    number_variant = flag_client.get_float_value("paymentFailure", 0.0)
    emit_raw_pii = flag_client.get_boolean_value("emitRawPii", False)

    if number_variant > 0 and random.random() < number_variant:
        span.set_attributes({"demo.user_context.loyalty_level": "gold"})
        raise ChargeError("Payment request failed. Invalid token. demo.user_context.loyalty_level=gold")

    if not request.HasField("credit_card"):
        raise TypeError("Cannot destructure property 'creditCardNumber' of 'request.creditCard' as it is null.")
    card_info = request.credit_card
    number = card_info.credit_card_number
    cvv = card_info.credit_card_cvv
    year = card_info.credit_card_expiration_year
    month = card_info.credit_card_expiration_month
    last_four_digits = number[-4:]
    transaction_id = str(uuid.uuid4())

    card = get_card_details(number)

    loyalty_level = random.choice(LOYALTY_LEVEL)

    attributes = {
        "demo.payment.card_type": card.card_type,
        "demo.payment.card_valid": card.valid,
        "demo.user_context.loyalty_level": loyalty_level,
    }
    if emit_raw_pii:
        attributes["demo.payment.card_number"] = number
        attributes["demo.payment.card_cvv"] = cvv
    span.set_attributes(attributes)

    if not card.valid:
        raise ChargeError("Credit card info is invalid.")

    if card.card_type not in ("visa", "mastercard"):
        raise ChargeError(
            f"Sorry, we cannot process {card.card_type} credit cards. Only VISA or MasterCard is accepted."
        )

    if (now.year * 12 + now.month) > (year * 12 + month):
        raise ChargeError(f"The credit card (ending {last_four_digits}) expired on {month}/{year}.")

    span.set_attribute("demo.payment.charged", not synthetic_request)

    enduser_id = baggage.get_baggage("enduser.id")
    if enduser_id:
        span.set_attribute("enduser.id", enduser_id)

    amount = request.amount
    logger.info(
        "Transaction complete.",
        extra={
            "transactionId": transaction_id,
            "cardType": card.card_type,
            "lastFourDigits": last_four_digits,
            "amount": {"units": amount.units, "nanos": amount.nanos, "currencyCode": amount.currency_code},
            "loyalty_level": loyalty_level,
        },
    )
    transactions_counter.add(1, {"demo.payment.currency": amount.currency_code})

    return transaction_id
