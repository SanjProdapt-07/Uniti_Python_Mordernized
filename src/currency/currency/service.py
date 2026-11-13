# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import math
import threading
import time

import grpc
from opentelemetry import metrics, propagate, trace
from opentelemetry._logs import SeverityNumber, get_logger
from opentelemetry.sdk.version import __version__ as sdk_version
from opentelemetry.trace import SpanKind, Status, StatusCode

import demo_pb2
import demo_pb2_grpc

SERVICE_NAME = "currency"

# Iteration order of the original std::unordered_map (libstdc++), which is the
# order GetSupportedCurrencies has always returned.
_INITIAL_RATES = {
    "ZAR": 16.0583,
    "ISK": 136.80,
    "ILS": 4.0875,
    "RON": 4.7463,
    "GBP": 0.85970,
    "BRL": 4.2682,
    "CZK": 25.592,
    "AUD": 1.6072,
    "BGN": 1.9558,
    "JPY": 126.40,
    "PHP": 59.083,
    "HUF": 315.51,
    "USD": 1.1305,
    "SGD": 1.5349,
    "IDR": 15999.40,
    "CHF": 1.1360,
    "TRY": 6.1247,
    "NZD": 1.6679,
    "THB": 36.012,
    "DKK": 7.4609,
    "PLN": 4.2996,
    "NOK": 9.8040,
    "HRK": 7.4210,
    "HKD": 8.8743,
    "EUR": 1.0,
    "RUB": 74.4208,
    "CAD": 1.5128,
    "INR": 79.4320,
    "SEK": 10.5375,
    "KRW": 1275.05,
    "CNY": 7.5857,
    "MXN": 21.7999,
    "MYR": 4.6289,
}

_INT64_MIN = -(2**63)


def _c_div(a: int, b: int) -> int:
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b >= 0) else -q


def _c_long(value: float) -> int:
    if math.isnan(value) or not (-(2.0**63) <= value < 2.0**63):
        return _INT64_MIN
    return int(value)


def _to_int32(value: int) -> int:
    return ((value + 2**31) % 2**32) - 2**31


def money_to_double(money) -> float:
    units = money.units
    nanos = money.nanos
    decimal = 0.0
    while nanos != 0:
        t = float(nanos - _c_div(nanos, 10) * 10) / 10
        nanos = _c_div(nanos, 10)
        decimal = decimal / 10 + t
    return float(units) + decimal


def double_to_money(value: float, currency_code: str):
    unit = _c_long(value)
    rem = value - unit
    nano = _c_long(rem * math.pow(10, 9))
    return demo_pb2.Money(currency_code=currency_code, units=unit, nanos=_to_int32(nano))


class RateTable:
    def __init__(self, rates=None):
        self._rates = dict(_INITIAL_RATES if rates is None else rates)
        self._lock = threading.Lock()

    def codes(self):
        with self._lock:
            return list(self._rates)

    def get(self, code: str) -> float:
        with self._lock:
            if code not in self._rates:
                self._rates = {code: 0.0, **self._rates}
            return self._rates[code]


def log_event(logger, severity, event_name, body, attributes=None):
    logger.emit(
        timestamp=time.time_ns(),
        severity_number=severity,
        severity_text=severity.name,
        event_name=event_name,
        body=body,
        attributes=attributes,
    )


class CurrencyService(demo_pb2_grpc.CurrencyServiceServicer):
    def __init__(self, rates: RateTable | None = None, version: str = "unknown"):
        self.rates = rates or RateTable()
        self.tracer = trace.get_tracer(SERVICE_NAME)
        self.logger = get_logger(SERVICE_NAME, sdk_version)
        meter = metrics.get_meter("demo.exchange.conversions", version)
        self.counter = meter.create_counter("demo.exchange.conversions_counter")

    def _start_span(self, name, method, context):
        carrier = {key: value for key, value in (context.invocation_metadata() or ()) if isinstance(value, str)}
        parent = propagate.extract(carrier)
        return self.tracer.start_span(
            name,
            context=parent,
            kind=SpanKind.SERVER,
            attributes={
                "rpc.system.name": "grpc",
                "rpc.method": method,
                "rpc.response.status_code": "OK",
            },
        )

    def GetSupportedCurrencies(self, request, context):
        span = self._start_span(
            "Currency/GetSupportedCurrencies", "oteldemo.CurrencyService/GetSupportedCurrencies", context
        )
        with trace.use_span(span, end_on_exit=True):
            span.add_event("Processing supported currencies request")
            response = demo_pb2.GetSupportedCurrenciesResponse(currency_codes=self.rates.codes())
            span.add_event("Currencies fetched, response sent back")
            span.set_status(Status(StatusCode.OK))
            log_event(
                self.logger,
                SeverityNumber.INFO,
                "currency.get_supported_currencies",
                "GetSupportedCurrencies successful",
            )
        return response

    def Convert(self, request, context):
        span = self._start_span("Currency/Convert", "oteldemo.CurrencyService/Convert", context)
        with trace.use_span(span, end_on_exit=False):
            span.add_event("Processing currency conversion request")
            try:
                from_money = getattr(request, "from")
                from_code = from_money.currency_code
                rate = self.rates.get(from_code)
                one_euro = _ieee_div(money_to_double(from_money), rate)

                to_code = request.to_code
                to_rate = self.rates.get(to_code)

                response = double_to_money(one_euro * to_rate, to_code)

                span.set_attribute("demo.exchange.from", from_code)
                span.set_attribute("demo.exchange.to", to_code)

                self.counter.add(1, {"demo.exchange.to": to_code})

                span.add_event("Conversion successful, response sent back")
                span.set_status(Status(StatusCode.OK))
                log_event(
                    self.logger,
                    SeverityNumber.INFO,
                    "currency.conversion",
                    "conversion successful",
                    {"currency.from": from_code, "currency.to": to_code},
                )
                span.end()
                return response
            except Exception:
                span.add_event("Conversion failed")
                span.set_status(Status(StatusCode.ERROR))
                log_event(self.logger, SeverityNumber.ERROR, "currency.conversion_failed", "conversion failure")
                span.end()
        context.abort(grpc.StatusCode.CANCELLED, "")


def _ieee_div(a: float, b: float) -> float:
    if b != 0.0:
        return a / b
    if math.isnan(a) or a == 0.0:
        return math.nan
    return math.copysign(math.inf, a) * math.copysign(1.0, b)
