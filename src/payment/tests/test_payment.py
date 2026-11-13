# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import json
import pathlib
import uuid
from datetime import datetime

import grpc
import pytest
from grpc_health.v1 import health_pb2, health_pb2_grpc
from openfeature import api
from openfeature.provider.in_memory_provider import InMemoryFlag, InMemoryProvider
from opentelemetry import baggage, context, trace
from opentelemetry.trace import StatusCode

import demo_pb2
import demo_pb2_grpc
from payment import charge as charge_module
from payment.card_validator import InvalidCardNumber, get_card_details
from payment.server import PaymentService, build_server, format_amount

DATA = pathlib.Path(__file__).parent / "data"
VALID_VISA = "4432-8015-6152-0454"
NOW = datetime(2026, 6, 15)


def flags(payment_failure=0.0, emit_raw_pii=False):
    return InMemoryProvider(
        {
            "paymentFailure": InMemoryFlag("v", {"v": float(payment_failure)}),
            "emitRawPii": InMemoryFlag("v", {"v": emit_raw_pii}),
        }
    )


@pytest.fixture
def flag_client():
    def make(**kwargs):
        api.set_provider(flags(**kwargs))
        return api.get_client()

    yield make
    api.clear_providers()


def request(number=VALID_VISA, cvv=672, year=2030, month=1, units=19, nanos=990000000, currency="USD"):
    return demo_pb2.ChargeRequest(
        amount=demo_pb2.Money(currency_code=currency, units=units, nanos=nanos),
        credit_card=demo_pb2.CreditCardInfo(
            credit_card_number=number,
            credit_card_cvv=cvv,
            credit_card_expiration_year=year,
            credit_card_expiration_month=month,
        ),
    )


def run_charge(req, client, bag=None):
    ctx = context.get_current()
    for key, value in (bag or {}).items():
        ctx = baggage.set_baggage(key, value, ctx)
    token = context.attach(ctx)
    try:
        return charge_module.charge(req, client, now=NOW)
    finally:
        context.detach(token)


def charge_span(spans):
    return next(s for s in spans.get_finished_spans() if s.name == "charge")


def test_card_validator_matches_simple_card_validator():
    for number, expected in json.loads((DATA / "card_validator_golden.json").read_text()):
        if expected[0] == "THROW":
            with pytest.raises(InvalidCardNumber):
                get_card_details(number)
            continue
        details = get_card_details(number)
        assert [details.card_type, details.valid] == expected, number


def test_amount_matches_js_tofixed():
    for (units, nanos), expected in json.loads((DATA / "amount_tofixed_golden.json").read_text()):
        assert format_amount(units, nanos) == expected


def test_successful_charge(telemetry, flag_client):
    spans, metric_reader, logs = telemetry
    transaction_id = run_charge(request(), flag_client(), {"enduser.id": "user-1"})
    uuid.UUID(transaction_id, version=4)

    span = charge_span(spans)
    assert span.attributes["demo.payment.card_type"] == "visa"
    assert span.attributes["demo.payment.card_valid"] is True
    assert span.attributes["demo.user_context.loyalty_level"] in charge_module.LOYALTY_LEVEL
    assert span.attributes["demo.payment.charged"] is True
    assert span.attributes["enduser.id"] == "user-1"
    assert "demo.payment.card_number" not in span.attributes
    assert "user_agent.synthetic.type" not in span.attributes
    assert span.status.status_code == StatusCode.UNSET

    (log,) = [l for l in logs.get_finished_logs() if l.log_record.body == "Transaction complete."]
    attrs = log.log_record.attributes
    assert attrs["transactionId"] == transaction_id
    assert attrs["cardType"] == "visa"
    assert attrs["lastFourDigits"] == "0454"
    assert dict(attrs["amount"]) == {"units": 19, "nanos": 990000000, "currencyCode": "USD"}

    found = False
    for rm in metric_reader.get_metrics_data().resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == "demo.payment.transactions":
                    assert sm.scope.name == "payment"
                    assert m.unit == "{transaction}"
                    found = any(p.attributes == {"demo.payment.currency": "USD"} for p in m.data.data_points)
    assert found


def test_synthetic_request_not_charged(telemetry, flag_client):
    spans, _, _ = telemetry
    run_charge(request(), flag_client(), {"synthetic_request": "true"})
    span = charge_span(spans)
    assert span.attributes["user_agent.synthetic.type"] == "test"
    assert span.attributes["demo.payment.charged"] is False


def test_emit_raw_pii(telemetry, flag_client):
    spans, _, _ = telemetry
    run_charge(request(), flag_client(emit_raw_pii=True))
    span = charge_span(spans)
    assert span.attributes["demo.payment.card_number"] == VALID_VISA
    assert span.attributes["demo.payment.card_cvv"] == 672


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"number": "4432-8015-6152-0455"}, "Credit card info is invalid."),
        (
            {"number": "378282246310005"},
            "Sorry, we cannot process amex credit cards. Only VISA or MasterCard is accepted.",
        ),
        (
            {"number": "4026000000000002"},
            "Sorry, we cannot process visa_electron credit cards. Only VISA or MasterCard is accepted.",
        ),
        ({"year": 2026, "month": 5}, "The credit card (ending 0454) expired on 5/2026."),
    ],
)
def test_rejections(telemetry, flag_client, kwargs, message):
    spans, _, _ = telemetry
    with pytest.raises(charge_module.ChargeError, match=None) as info:
        run_charge(request(**kwargs), flag_client())
    assert str(info.value) == message
    span = charge_span(spans)
    assert span.status.status_code == StatusCode.ERROR
    assert span.status.description == message
    assert span.attributes["error.type"] == "Error"
    assert span.events[0].name == "exception"
    assert span.events[0].attributes["exception.type"] == "Error"


def test_current_month_is_not_expired(flag_client):
    run_charge(request(year=2026, month=6), flag_client())


def test_mastercard_accepted(flag_client):
    run_charge(request(number="5555555555554444"), flag_client())


def test_payment_failure_flag(telemetry, flag_client):
    spans, _, _ = telemetry
    with pytest.raises(charge_module.ChargeError) as info:
        run_charge(request(), flag_client(payment_failure=1))
    assert str(info.value) == "Payment request failed. Invalid token. demo.user_context.loyalty_level=gold"
    span = charge_span(spans)
    assert span.attributes["demo.user_context.loyalty_level"] == "gold"
    assert "demo.payment.card_type" not in span.attributes


def test_payment_failure_flag_is_probabilistic(flag_client, monkeypatch):
    client = flag_client(payment_failure=0.25)
    monkeypatch.setattr(charge_module.random, "random", lambda: 0.3)
    run_charge(request(), client)
    monkeypatch.setattr(charge_module.random, "random", lambda: 0.2)
    with pytest.raises(charge_module.ChargeError):
        run_charge(request(), client)


class AbortContext:
    def abort(self, code, details):
        self.code, self.details = code, details
        raise grpc.RpcError(details)


def test_servicer_sets_server_span_attributes(telemetry, flag_client):
    spans, _, _ = telemetry
    service = PaymentService(flag_client())
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("server"):
        response = service.Charge(request(units=0, nanos=125000000), AbortContext())
    assert response.transaction_id
    server = next(s for s in spans.get_finished_spans() if s.name == "server")
    assert server.attributes["demo.payment.amount"] == "0.13"
    assert charge_span(spans).parent.span_id == server.context.span_id

    ctx = AbortContext()
    with tracer.start_as_current_span("server"):
        with pytest.raises(grpc.RpcError):
            service.Charge(request(number="378282246310005"), ctx)
    assert ctx.code == grpc.StatusCode.UNKNOWN
    assert ctx.details.startswith("Sorry, we cannot process amex")
    server = [s for s in spans.get_finished_spans() if s.name == "server"][-1]
    assert server.status.status_code == StatusCode.ERROR
    assert server.attributes["error.type"] == "Error"


def test_missing_credit_card_is_type_error(flag_client):
    ctx = AbortContext()
    with trace.get_tracer("test").start_as_current_span("server"):
        with pytest.raises(grpc.RpcError):
            PaymentService(flag_client()).Charge(
                demo_pb2.ChargeRequest(amount=demo_pb2.Money(currency_code="USD", units=1)), ctx
            )
    assert ctx.code == grpc.StatusCode.UNKNOWN


def test_over_the_wire_with_server_instrumentation(telemetry, flag_client):
    from opentelemetry.instrumentation.grpc import GrpcInstrumentorServer

    spans, _, _ = telemetry
    instrumentor = GrpcInstrumentorServer()
    instrumentor.instrument()
    try:
        server, port = build_server(PaymentService(flag_client()), "127.0.0.1:0")
    finally:
        instrumentor.uninstrument()
    server.start()
    try:
        with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = demo_pb2_grpc.PaymentServiceStub(channel)
            metadata = (("baggage", "synthetic_request=true,enduser.id=u-9"),)
            response = stub.Charge(request(), metadata=metadata)
            assert response.transaction_id
            with pytest.raises(grpc.RpcError) as info:
                stub.Charge(request(number="4432-8015-6152-0455"))
            assert info.value.code() == grpc.StatusCode.UNKNOWN
            assert info.value.details() == "Credit card info is invalid."
            health = health_pb2_grpc.HealthStub(channel).Check(health_pb2.HealthCheckRequest())
            assert health.status == health_pb2.HealthCheckResponse.SERVING
    finally:
        server.stop(None)

    server_spans = [s for s in spans.get_finished_spans() if s.kind == trace.SpanKind.SERVER]
    assert server_spans[0].attributes["demo.payment.amount"] == "19.99"
    first_charge = [s for s in spans.get_finished_spans() if s.name == "charge"][0]
    assert first_charge.parent.span_id == server_spans[0].context.span_id
    assert first_charge.attributes["demo.payment.charged"] is False
    assert first_charge.attributes["enduser.id"] == "u-9"
    assert server_spans[1].status.status_code == StatusCode.ERROR
