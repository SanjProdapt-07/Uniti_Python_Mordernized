# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import pathlib

import grpc
import pytest
from grpc_health.v1 import health_pb2, health_pb2_grpc
from opentelemetry.trace import SpanContext, SpanKind, StatusCode, TraceFlags

import demo_pb2
import demo_pb2_grpc
from conftest import traceparent_metadata
from currency.__main__ import build_server
from currency.service import CurrencyService, RateTable, double_to_money, money_to_double

GOLDEN = pathlib.Path(__file__).parent / "data" / "convert_golden.txt"

ORIGINAL_ORDER = (
    "ZAR ISK ILS RON GBP BRL CZK AUD BGN JPY PHP HUF USD SGD IDR CHF TRY NZD THB DKK PLN NOK HRK HKD "
    "EUR RUB CAD INR SEK KRW CNY MXN MYR"
).split()


def convert(service, ctx, from_code, units, nanos, to_code):
    request = demo_pb2.CurrencyConversionRequest(
        **{"from": demo_pb2.Money(currency_code=from_code, units=units, nanos=nanos)}, to_code=to_code
    )
    return service.Convert(request, ctx)


def test_supported_currencies_in_original_order(telemetry, fake_context):
    spans, _, logs = telemetry
    response = CurrencyService().GetSupportedCurrencies(demo_pb2.Empty(), fake_context())
    assert list(response.currency_codes) == ORIGINAL_ORDER

    (span,) = spans.get_finished_spans()
    assert span.name == "Currency/GetSupportedCurrencies"
    assert span.kind == SpanKind.SERVER
    assert span.status.status_code == StatusCode.OK
    assert dict(span.attributes) == {
        "rpc.system.name": "grpc",
        "rpc.method": "oteldemo.CurrencyService/GetSupportedCurrencies",
        "rpc.response.status_code": "OK",
    }
    assert [e.name for e in span.events] == [
        "Processing supported currencies request",
        "Currencies fetched, response sent back",
    ]
    (log,) = logs.get_finished_logs()
    assert log.log_record.event_name == "currency.get_supported_currencies"
    assert log.log_record.body == "GetSupportedCurrencies successful"
    assert log.log_record.span_id == span.context.span_id


def test_convert_matches_original_cpp_results():
    service = CurrencyService()
    for line in GOLDEN.read_text().splitlines():
        from_code, units, nanos, to_code, exp_units, exp_nanos = line.split()
        result = convert(service, _NoMeta(), from_code, int(units), int(nanos), to_code)
        assert (result.units, result.nanos, result.currency_code) == (int(exp_units), int(exp_nanos), to_code), line


class _NoMeta:
    def invocation_metadata(self):
        return ()


def test_nanos_digit_quirk_is_preserved():
    assert money_to_double(demo_pb2.Money(units=1, nanos=50000000)) == 1.5
    assert money_to_double(demo_pb2.Money(units=1, nanos=990000000)) == 1.99
    assert money_to_double(demo_pb2.Money(units=0, nanos=-5)) == -0.5
    assert double_to_money(float("inf"), "X").units == -(2**63)


def test_convert_telemetry_and_parent(telemetry, fake_context):
    spans, metrics_reader, logs = telemetry
    parent = SpanContext(trace_id=0xABC, span_id=0xDEF, is_remote=True, trace_flags=TraceFlags(1))
    ctx = fake_context(traceparent_metadata(parent))
    result = convert(CurrencyService(version="9.9"), ctx, "USD", 100, 0, "EUR")
    assert result.currency_code == "EUR"
    assert result.units == 88

    (span,) = spans.get_finished_spans()
    assert span.name == "Currency/Convert"
    assert span.parent.span_id == 0xDEF
    assert span.context.trace_id == 0xABC
    assert span.attributes["demo.exchange.from"] == "USD"
    assert span.attributes["demo.exchange.to"] == "EUR"
    assert span.attributes["rpc.method"] == "oteldemo.CurrencyService/Convert"
    assert [e.name for e in span.events] == [
        "Processing currency conversion request",
        "Conversion successful, response sent back",
    ]
    (log,) = logs.get_finished_logs()
    assert log.log_record.event_name == "currency.conversion"
    assert log.log_record.body == "conversion successful"
    assert dict(log.log_record.attributes) == {"currency.from": "USD", "currency.to": "EUR"}

    points = {}
    for rm in metrics_reader.get_metrics_data().resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == "demo.exchange.conversions_counter":
                    assert sm.scope.name == "demo.exchange.conversions"
                    for p in m.data.data_points:
                        points[p.attributes["demo.exchange.to"]] = p.value
    assert points.get("EUR", 0) >= 1


def test_unknown_currency_behaves_like_original_map():
    rates = RateTable()
    service = CurrencyService(rates=rates)
    result = convert(service, _NoMeta(), "USD", 10, 0, "XYZ")
    assert (result.units, result.nanos, result.currency_code) == (0, 0, "XYZ")
    assert rates.codes()[0] == "XYZ"
    result = convert(service, _NoMeta(), "QQQ", 10, 0, "EUR")
    assert (result.units, result.nanos) == (-(2**63), 0)


def test_failure_path_returns_cancelled(telemetry, fake_context, monkeypatch):
    spans, _, logs = telemetry
    service = CurrencyService()
    monkeypatch.setattr(service.rates, "get", lambda code: (_ for _ in ()).throw(ValueError("boom")))
    ctx = fake_context()
    with pytest.raises(RuntimeError):
        convert(service, ctx, "USD", 1, 0, "EUR")
    assert ctx.code == grpc.StatusCode.CANCELLED
    (span,) = spans.get_finished_spans()
    assert span.status.status_code == StatusCode.ERROR
    assert span.events[-1].name == "Conversion failed"
    (log,) = logs.get_finished_logs()
    assert log.log_record.event_name == "currency.conversion_failed"
    assert log.log_record.body == "conversion failure"


def test_over_the_wire():
    server, port = build_server(CurrencyService(), "127.0.0.1:0")
    server.start()
    try:
        with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = demo_pb2_grpc.CurrencyServiceStub(channel)
            assert list(stub.GetSupportedCurrencies(demo_pb2.Empty()).currency_codes) == ORIGINAL_ORDER
            money = stub.Convert(
                demo_pb2.CurrencyConversionRequest(
                    **{"from": demo_pb2.Money(currency_code="EUR", units=2, nanos=0)}, to_code="USD"
                )
            )
            assert (money.currency_code, money.units, money.nanos) == ("USD", 2, 261000000)
            health = health_pb2_grpc.HealthStub(channel).Check(health_pb2.HealthCheckRequest())
            assert health.status == health_pb2.HealthCheckResponse.SERVING
    finally:
        server.stop(None)
