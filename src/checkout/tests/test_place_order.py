# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import logging
import time

import demo_pb2
import grpc
import httpx
import pytest
from opentelemetry import baggage, context
from opentelemetry.trace import SpanKind, StatusCode

from conftest import HAVE_TRACING_HOOK, METRIC_READER, body_json, order_request, set_flags


def _span(spans, name):
    found = [s for s in spans.get_finished_spans() if s.name == name]
    assert found, f"span {name!r} not found in {[s.name for s in spans.get_finished_spans()]}"
    return found[-1]


def _server_span(spans):
    found = [
        s for s in spans.get_finished_spans()
        if s.name == "/oteldemo.CheckoutService/PlaceOrder" and s.kind == SpanKind.SERVER
    ]
    assert found
    return found[-1]


def _wait_for(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _messages(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "checkout"]


def test_place_order_happy_path(harness, spans, caplog):
    caplog.set_level(logging.INFO, logger="checkout")
    resp = harness.client.PlaceOrder(order_request(), timeout=10)
    order = resp.order

    assert harness.backend.names() == [
        "GetCart", "GetProduct", "Convert", "GetProduct", "Convert", "Convert", "Charge", "EmptyCart",
    ]
    assert [r.url.path for r in harness.http.requests] == ["/get-quote", "/ship-order", "/send_order_confirmation"]

    assert order.shipping_tracking_id == "TRACK-1"
    assert len(order.order_id) == 36 and order.order_id[14] == "1"
    assert order.shipping_cost == demo_pb2.Money(currency_code="USD", units=8, nanos=990000000)
    assert order.shipping_address.city == "Mountain View"
    assert [(i.item.product_id, i.item.quantity, i.cost.units, i.cost.nanos) for i in order.items] == [
        ("OLJCESPC7Z", 2, 101, 960000000),
        ("66VCHSJNUP", 1, 349, 950000000),
    ]

    charge = [c for c in harness.backend.calls if c[0] == "Charge"][0][1]
    assert charge.amount == demo_pb2.Money(currency_code="USD", units=562, nanos=860000000)
    assert charge.credit_card.credit_card_number == "4432-8015-6152-0454"

    quote_req = harness.http.by_path("/get-quote")[0]
    assert quote_req.headers["content-type"] == "application/json"
    assert quote_req.content == (
        b'{"address":{"street_address":"1600 Amphitheatre Parkway","city":"Mountain View","state":"CA",'
        b'"country":"United States","zip_code":"94043"},'
        b'"items":[{"product_id":"OLJCESPC7Z","quantity":2},{"product_id":"66VCHSJNUP","quantity":1}]}'
    )
    assert harness.http.by_path("/ship-order")[0].content == quote_req.content
    email = body_json(harness.http.by_path("/send_order_confirmation")[0])
    assert email["email"] == "someone@example.com"
    assert email["order"]["order_id"] == order.order_id
    assert email["order"]["shipping_cost"] == {"currency_code": "USD", "units": 8, "nanos": 990000000}
    assert email["order"]["items"][0] == {
        "item": {"product_id": "OLJCESPC7Z", "quantity": 2},
        "cost": {"currency_code": "USD", "units": 101, "nanos": 960000000},
    }

    server = _server_span(spans)
    attrs = dict(server.attributes)
    assert attrs["user.id"] == "user-1"
    assert attrs["demo.user_context.selected_currency"] == "USD"
    assert attrs["demo.order.id"] == order.order_id
    assert attrs["demo.shipping.amount"] == 8.99
    assert attrs["demo.order.amount"] == 562.86
    assert attrs["demo.order.items.count"] == 2
    assert attrs["demo.shipping.tracking.id"] == "TRACK-1"
    assert "user.email" not in attrs and "user_agent.synthetic.type" not in attrs
    events = [e for e in server.events if e.name in ("prepared", "charged", "shipped")]
    assert [e.name for e in events] == ["prepared", "charged", "shipped"]
    assert dict(events[1].attributes) == {"demo.payment.transaction.id": "tx-123"}
    assert dict(events[2].attributes) == {"demo.shipping.tracking.id": "TRACK-1"}
    if HAVE_TRACING_HOOK:
        flag_events = [e.attributes["feature_flag.key"] for e in server.events if e.name == "feature_flag.evaluation"]
        assert flag_events == ["emitRawPii", "paymentUnreachable", "kafkaQueueProblems"]

    prep = _span(spans, "prepareOrderItemsAndShippingQuoteFromCart")
    assert prep.kind == SpanKind.INTERNAL
    assert prep.parent.span_id == server.context.span_id
    assert dict(prep.attributes) == {
        "demo.shipping.amount": 8.99, "demo.cart.items.count": 3, "demo.order.items.count": 2,
    }

    by_id = {s.context.span_id: s for s in spans.get_finished_spans()}
    http_spans = [s for s in spans.get_finished_spans() if s.kind == SpanKind.CLIENT and s.name.startswith("POST")]
    assert len(http_spans) == 3
    assert by_id[http_spans[0].parent.span_id].name == prep.name
    assert all(by_id[s.parent.span_id] is server for s in http_spans[1:])
    for request in harness.http.requests:
        assert request.headers["traceparent"].split("-")[1] == format(server.context.trace_id, "032x")
    get_cart_md = harness.backend.calls[0][2]
    assert get_cart_md["traceparent"].split("-")[1] == format(server.context.trace_id, "032x")

    producer = _span(spans, "orders publish")
    assert producer.kind == SpanKind.PRODUCER
    assert producer.parent.span_id == server.context.span_id
    pattrs = dict(producer.attributes)
    assert pattrs["peer.service"] == "kafka"
    assert pattrs["network.transport"] == "tcp"
    assert pattrs["messaging.system"] == "kafka"
    assert pattrs["messaging.destination.name"] == "orders"
    assert pattrs["messaging.operation"] == "publish"
    assert pattrs["messaging.kafka.destination.partition"] == 0
    assert pattrs["messaging.kafka.producer.success"] is True
    assert isinstance(pattrs["messaging.kafka.producer.duration_ms"], int)
    assert pattrs["messaging.kafka.message.offset"] == 0
    assert producer.status.status_code == StatusCode.UNSET

    assert len(harness.producer.messages) == 1
    topic, value, headers = harness.producer.messages[0]
    assert topic == "orders"
    parsed = demo_pb2.OrderResult()
    parsed.ParseFromString(value)
    assert parsed == order
    headers = dict(headers)
    assert headers["traceparent"].decode().startswith(
        f"00-{format(producer.context.trace_id, '032x')}-{format(producer.context.span_id, '016x')}-"
    )

    msgs = _messages(caplog)
    assert msgs[0] == "[PlaceOrder]"
    assert "payment went through" in msgs
    assert "order placed" in msgs
    assert "order confirmation email sent" in msgs
    assert "sending to postProcessor" in msgs
    assert any(m.startswith("Successful to write message. offset: 0, duration: ") for m in msgs)
    placed = [r for r in caplog.records if r.getMessage() == "order placed"][0]
    assert getattr(placed, "demo.order.id") == order.order_id
    assert getattr(placed, "demo.order.amount") == 562.86
    first = [r for r in caplog.records if r.getMessage() == "[PlaceOrder]"][0]
    assert (first.user_id, first.user_currency) == ("user-1", "USD")
    tx = [r for r in caplog.records if r.getMessage() == "payment went through"][0]
    assert tx.transaction_id == "tx-123"


def test_kafka_disabled_when_addr_empty(harness, spans, caplog):
    caplog.set_level(logging.INFO, logger="checkout")
    harness.svc.kafka_broker_svc_addr = ""
    harness.client.PlaceOrder(order_request(), timeout=10)
    assert harness.producer.messages == []
    assert "sending to postProcessor" not in _messages(caplog)
    assert not [s for s in spans.get_finished_spans() if s.name == "orders publish"]


def test_emit_raw_pii(harness, spans):
    set_flags(emitRawPii=True)
    harness.client.PlaceOrder(order_request(), timeout=10)
    attrs = dict(_server_span(spans).attributes)
    assert attrs["user.email"] == "someone@example.com"
    assert attrs["demo.payment.card_number"] == "4432-8015-6152-0454"
    assert attrs["demo.payment.card_cvv"] == 672


def test_synthetic_baggage(harness, spans):
    token = context.attach(baggage.set_baggage("synthetic_request", "true"))
    try:
        harness.client.PlaceOrder(order_request(), timeout=10)
    finally:
        context.detach(token)
    assert _server_span(spans).attributes["user_agent.synthetic.type"] == "test"


def test_kafka_queue_problems_floods_topic(harness, caplog):
    caplog.set_level(logging.INFO, logger="checkout")
    set_flags(kafkaQueueProblems=5)
    harness.client.PlaceOrder(order_request(), timeout=10)
    assert _wait_for(lambda: len(harness.producer.messages) == 6)
    first = harness.producer.messages[0]
    assert all(m == first for m in harness.producer.messages)
    msgs = _messages(caplog)
    assert "Warning: FeatureFlag 'kafkaQueueProblems' is activated, overloading queue now." in msgs
    assert "Done with #5 messages for overload simulation." in msgs


def test_payment_unreachable(harness, spans):
    set_flags(paymentUnreachable=True)
    with pytest.raises(grpc.RpcError) as exc:
        harness.client.PlaceOrder(order_request(), timeout=30)
    assert exc.value.code() == grpc.StatusCode.INTERNAL
    assert exc.value.details().startswith(
        "failed to charge card: could not charge the card: rpc error: code = Unavailable desc = "
    )
    assert harness.bad_channels == ["badAddress:50051"]
    assert "Charge" not in harness.backend.names()
    server = _server_span(spans)
    names = [e.name for e in server.events if e.name in ("prepared", "exception")]
    assert names[0] == "prepared" and "exception" in names


@pytest.mark.parametrize("method,code,expected", [
    ("GetCart", grpc.StatusCode.NOT_FOUND,
     "cart failure: failed to get user cart during checkout: rpc error: code = NotFound desc = boom"),
    ("GetProduct", grpc.StatusCode.NOT_FOUND, 'failed to prepare order: failed to get product #"OLJCESPC7Z"'),
    ("Convert", grpc.StatusCode.UNAVAILABLE, 'failed to prepare order: failed to convert price of "OLJCESPC7Z" to USD'),
])
def test_prepare_failures_are_internal(harness, spans, method, code, expected):
    harness.backend.fail[method] = (code, "boom")
    with pytest.raises(grpc.RpcError) as exc:
        harness.client.PlaceOrder(order_request(), timeout=10)
    assert exc.value.code() == grpc.StatusCode.INTERNAL
    assert exc.value.details() == expected
    server = _server_span(spans)
    assert "prepared" not in [e.name for e in server.events]
    exc_events = [e for e in server.events if e.name == "exception"]
    assert exc_events and exc_events[0].attributes["exception.message"] == expected
    prep = _span(spans, "prepareOrderItemsAndShippingQuoteFromCart")
    assert prep.status.status_code == StatusCode.UNSET and not prep.events


def test_shipping_conversion_failure(harness):
    count = {"n": 0}

    def convert(result, context):
        count["n"] += 1
        if count["n"] == 3:
            context.abort(grpc.StatusCode.UNAVAILABLE, "down")

    harness.backend.convert_hook = convert
    with pytest.raises(grpc.RpcError) as exc:
        harness.client.PlaceOrder(order_request(), timeout=10)
    assert exc.value.code() == grpc.StatusCode.INTERNAL
    assert exc.value.details() == (
        "failed to convert shipping cost to currency: failed to convert currency: "
        "rpc error: code = Unavailable desc = down"
    )


@pytest.mark.parametrize("response,expected", [
    (lambda: httpx.Response(500, text="x"), "shipping quote failure: failed POST to shipping service: expected 200, got 500"),
    (lambda: httpx.Response(200, json={}), "shipping quote failure: shipping quote missing cost_usd field"),
    (lambda: httpx.Response(200, json={"cost_usd": None}), "shipping quote failure: shipping quote missing cost_usd field"),
    (lambda: httpx.Response(200, text="not json"), "shipping quote failure: failed to unmarshal shipping quote: "),
    (lambda: httpx.Response(200, json={"cost_usd": {"units": 1.5}}), "shipping quote failure: failed to unmarshal shipping quote: json: cannot unmarshal number 1.5"),
])
def test_quote_failures(harness, response, expected):
    harness.http.responses["/get-quote"] = response
    with pytest.raises(grpc.RpcError) as exc:
        harness.client.PlaceOrder(order_request(), timeout=10)
    assert exc.value.code() == grpc.StatusCode.INTERNAL
    assert exc.value.details().startswith(expected)
    assert "Charge" not in harness.backend.names()


def test_quote_transport_error(harness):
    def boom():
        raise httpx.ConnectError("connection refused")

    harness.http.responses["/get-quote"] = boom
    with pytest.raises(grpc.RpcError) as exc:
        harness.client.PlaceOrder(order_request(), timeout=10)
    assert exc.value.details() == (
        'shipping quote failure: failed POST to shipping service: '
        'Post "http://shipping:50050/get-quote": connection refused'
    )


def test_quote_accepts_case_insensitive_keys(harness):
    harness.http.responses["/get-quote"] = lambda: httpx.Response(
        200, json={"COST_USD": {"Currency_Code": "USD", "UNITS": 3}}
    )
    resp = harness.client.PlaceOrder(order_request(), timeout=10)
    assert resp.order.shipping_cost == demo_pb2.Money(currency_code="USD", units=3)


def test_charge_declined(harness, spans):
    harness.backend.fail["Charge"] = (grpc.StatusCode.UNKNOWN, "Credit card info is invalid.")
    with pytest.raises(grpc.RpcError) as exc:
        harness.client.PlaceOrder(order_request(), timeout=10)
    assert exc.value.code() == grpc.StatusCode.INTERNAL
    assert exc.value.details() == (
        "failed to charge card: could not charge the card: rpc error: code = Unknown desc = Credit card info is invalid."
    )
    assert "EmptyCart" not in harness.backend.names()
    assert harness.http.by_path("/ship-order") == []


@pytest.mark.parametrize("response,expected", [
    (lambda: httpx.Response(503), "shipping error: failed POST to shipping service: expected 200, got 503"),
    (lambda: httpx.Response(200, json={"tracking_id": ""}), "shipping error: ship order response missing tracking_id field"),
    (lambda: httpx.Response(200, json={"tracking_id": 5}), "shipping error: failed to unmarshal ship order response: json: cannot unmarshal number"),
])
def test_ship_order_failures_are_unavailable(harness, spans, response, expected):
    harness.http.responses["/ship-order"] = response
    with pytest.raises(grpc.RpcError) as exc:
        harness.client.PlaceOrder(order_request(), timeout=10)
    assert exc.value.code() == grpc.StatusCode.UNAVAILABLE
    assert exc.value.details().startswith(expected)
    assert "Charge" in harness.backend.names()
    assert "EmptyCart" not in harness.backend.names()
    assert [e.name for e in _server_span(spans).events if e.name in ("charged", "shipped")] == ["charged"]


def test_empty_cart_failure_is_ignored(harness):
    harness.backend.fail["EmptyCart"] = (grpc.StatusCode.UNAVAILABLE, "down")
    resp = harness.client.PlaceOrder(order_request(), timeout=10)
    assert resp.order.shipping_tracking_id == "TRACK-1"


def test_email_failure_is_logged_not_returned(harness, caplog):
    caplog.set_level(logging.INFO, logger="checkout")
    harness.http.responses["/send_order_confirmation"] = lambda: httpx.Response(500)
    resp = harness.client.PlaceOrder(order_request(), timeout=10)
    assert resp.order.order_id
    warn = [r for r in caplog.records if r.levelno == logging.WARNING and r.name == "checkout"]
    assert [r.getMessage() for r in warn] == [
        "failed to send order confirmation: failed POST to email service: expected 200, got 500"
    ]
    assert harness.producer.messages


def test_email_request_has_bounded_timeout(harness):
    harness.client.PlaceOrder(order_request(), timeout=10)
    request = harness.http.by_path("/send_order_confirmation")[0]
    assert all(v is not None and 0 < v <= 1.0 for v in request.extensions["timeout"].values())
    quote = harness.http.by_path("/get-quote")[0]
    assert all(v is not None and 0 < v <= 10 for v in quote.extensions["timeout"].values())


def test_no_client_deadline_means_no_downstream_deadline(harness):
    harness.client.PlaceOrder(order_request())
    assert all(c[3] > 10**9 for c in harness.backend.calls)
    quote = harness.http.by_path("/get-quote")[0]
    assert all(v is None for v in quote.extensions["timeout"].values())
    email = harness.http.by_path("/send_order_confirmation")[0]
    assert all(v == 1.0 for v in email.extensions["timeout"].values())


def test_client_deadline_propagates_to_grpc_calls(harness):
    harness.client.PlaceOrder(order_request(), timeout=10)
    assert all(c[3] is not None and 0 < c[3] <= 10 for c in harness.backend.calls)


def test_kafka_delivery_error(harness, spans, caplog):
    caplog.set_level(logging.INFO, logger="checkout")
    harness.producer.mode = "error"
    harness.client.PlaceOrder(order_request(), timeout=10)
    producer = _span(spans, "orders publish")
    assert producer.status.status_code == StatusCode.ERROR
    assert producer.status.description == "Local: Message timed out"
    assert producer.attributes["messaging.kafka.producer.success"] is False
    assert "messaging.kafka.message.offset" not in producer.attributes
    assert "Failed to write message: Local: Message timed out" in _messages(caplog)


def test_kafka_wait_until_deadline(harness, spans, caplog):
    caplog.set_level(logging.INFO, logger="checkout")
    harness.producer.mode = "never"
    set_flags(kafkaQueueProblems=2)
    with pytest.raises(grpc.RpcError) as exc:
        harness.client.PlaceOrder(order_request(), timeout=1.5)
    assert exc.value.code() == grpc.StatusCode.DEADLINE_EXCEEDED
    assert _wait_for(lambda: any(s.name == "orders publish" for s in spans.get_finished_spans()), 3)
    producer = _span(spans, "orders publish")
    assert producer.status.status_code == StatusCode.ERROR
    assert producer.status.description == "Context cancelled: context deadline exceeded"
    msgs = _messages(caplog)
    assert "Context canceled before success message received: context deadline exceeded" in msgs
    assert "Done with #2 messages for overload simulation." in msgs


def test_kafka_buffer_full_until_deadline(harness, spans, caplog):
    caplog.set_level(logging.INFO, logger="checkout")
    harness.producer.buffer_full = True
    set_flags(kafkaQueueProblems=2)
    with pytest.raises(grpc.RpcError) as exc:
        harness.client.PlaceOrder(order_request(), timeout=1.5)
    assert exc.value.code() == grpc.StatusCode.DEADLINE_EXCEEDED
    assert _wait_for(lambda: any(s.name == "orders publish" for s in spans.get_finished_spans()), 3)
    producer = _span(spans, "orders publish")
    assert producer.status.description == "Failed to send: context deadline exceeded"
    msgs = _messages(caplog)
    assert "Failed to send message to Kafka within context deadline: context deadline exceeded" in msgs
    assert not any(m.startswith("Done with") for m in msgs)


def test_currency_mismatch_fails_without_crashing(harness):
    def wrong_currency(result, context):
        result.currency_code = "CAD"

    harness.backend.convert_hook = wrong_currency
    with pytest.raises(grpc.RpcError) as exc:
        harness.client.PlaceOrder(order_request(), timeout=10)
    assert exc.value.code() == grpc.StatusCode.UNKNOWN
    assert "Charge" not in harness.backend.names()


def test_rpc_duration_metrics(harness):
    harness.client.PlaceOrder(order_request(), timeout=10)
    data = METRIC_READER.get_metrics_data()
    points = {}
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                assert metric.unit == "ms" if metric.name.startswith("rpc.") else True
                points.setdefault(metric.name, []).extend(metric.data.data_points)
    server = [dict(p.attributes) for p in points["rpc.server.duration"]]
    assert {"rpc.system": "grpc", "rpc.service": "oteldemo.CheckoutService", "rpc.method": "PlaceOrder",
            "rpc.grpc.status_code": 0} in server
    client = [dict(p.attributes) for p in points["rpc.client.duration"]]
    assert {(c["rpc.service"], c["rpc.method"]) for c in client} >= {
        ("oteldemo.CartService", "GetCart"), ("oteldemo.PaymentService", "Charge"),
        ("oteldemo.CurrencyService", "Convert"), ("oteldemo.ProductCatalogService", "GetProduct"),
    }


def test_failed_rpc_status_code_in_metrics(harness):
    harness.backend.fail["GetCart"] = (grpc.StatusCode.NOT_FOUND, "x")
    with pytest.raises(grpc.RpcError):
        harness.client.PlaceOrder(order_request(), timeout=10)
    data = METRIC_READER.get_metrics_data()
    server = [
        dict(p.attributes)
        for rm in data.resource_metrics for sm in rm.scope_metrics for m in sm.metrics
        if m.name == "rpc.server.duration" for p in m.data.data_points
    ]
    assert any(a["rpc.grpc.status_code"] == grpc.StatusCode.INTERNAL.value[0] for a in server)
