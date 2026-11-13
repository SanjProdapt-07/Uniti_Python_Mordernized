# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import json
import time
import uuid

import httpx
import pytest
from conftest import LOGS, SPANS, metric_points
from fastapi.testclient import TestClient

from shipping_service.app import create_app
from shipping_service.flags import IntResolution
from shipping_service.quote import Quote, QuoteClient, create_quote_from_float


class FakeFlags:
    def __init__(self, value=0, variant="off", error=None):
        self.result = IntResolution(value, variant, error)
        self.calls = []

    def resolve_int(self, key, default):
        self.calls.append((key, default))
        return self.result


class FakeQuote:
    def __init__(self, body=b"8.99", status=200, raise_exc=None):
        self.body, self.status, self.raise_exc = body, status, raise_exc
        self.requests = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raise_exc:
            raise self.raise_exc
        return httpx.Response(self.status, content=self.body)


def make_client(flags=None, quote=None):
    quote = quote or FakeQuote()
    app = create_app(flags or FakeFlags(), QuoteClient(httpx.MockTransport(quote.handler)))
    return TestClient(app), quote


def address(country):
    return {"street_address": "123 Main St", "city": "Anytown", "state": "CA", "country": country, "zip_code": "00000"}


def logs_named(name):
    return [l.log_record for l in LOGS.get_finished_logs() if l.log_record.event_name == name]


@pytest.mark.parametrize(
    "value,dollars,cents",
    [(10.99, 10, 99), (8.99, 8, 99), (35.96, 35, 96), (71.92, 71, 92), (0.01, 0, 1), (100.0, 100, 0),
     (-8.99, 0, 0), (float("nan"), 0, 0), (1e30, (2**64 - 1) // 100, (2**64 - 1) % 100)],
)
def test_create_quote_from_float(value, dollars, cents):
    assert create_quote_from_float(value) == Quote(dollars, cents)


def test_quote_display():
    assert str(Quote(10, 99)) == "10.99"
    assert str(Quote(0, 1)) == "0.01"
    assert str(Quote(10, 5)) == "10.05"
    assert str(Quote(100, 0)) == "100.00"


def test_get_quote(monkeypatch):
    monkeypatch.setenv("QUOTE_ADDR", "http://quote-test:1234")
    client, quote = make_client(quote=FakeQuote(b"26.97"))
    before = sum(p.value for _, _, p in metric_points("demo.shipping.items_shipped"))
    resp = client.post("/get-quote", json={"items": [{"product_id": "A", "quantity": 1}, {"productId": "B", "quantity": 2}], "address": address("US")})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/json"
    assert resp.content == b'{"cost_usd":{"currency_code":"USD","units":26,"nanos":970000000}}'

    (req,) = quote.requests
    assert str(req.url) == "http://quote-test:1234/getquote"
    assert req.method == "POST"
    assert json.loads(req.content) == {"numberOfItems": 3}
    assert req.headers["content-type"] == "application/json"

    points = metric_points("demo.shipping.items_shipped")
    scope, metric, _ = points[0]
    assert scope == "otel_demo.shipping.quote"
    assert metric.unit == "{item}"
    assert sum(p.value for _, _, p in points) == before + 3

    server = next(s for s in SPANS.get_finished_spans() if s.name == "POST /get-quote")
    assert server.attributes["demo.shipping.cost.total"] == "26.97"
    (event,) = [e for e in server.events if e.name == "shipping.quote.received"]
    assert event.attributes["demo.shipping.cost.total"] == "26.97"

    (requested,) = logs_named("shipping.quote.requested")
    assert requested.body == "Requesting quote"
    assert requested.attributes["quote_service_addr"] == "http://quote-test:1234/getquote"
    (sent,) = logs_named("shipping.quote.sent")
    assert sent.body == "Sending Quote"
    assert sent.severity_text == "INFO"
    assert dict(sent.attributes) == {"dollars": 26, "cents": 97}
    assert sent.trace_id == server.context.trace_id


def test_get_quote_default_quote_addr(monkeypatch):
    monkeypatch.delenv("QUOTE_ADDR", raising=False)
    client, quote = make_client()
    client.post("/get-quote", json={"items": []})
    assert str(quote.requests[0].url) == "http://quote:8090/getquote"
    assert json.loads(quote.requests[0].content) == {"numberOfItems": 0}


@pytest.mark.parametrize(
    "fake,fragment",
    [
        (FakeQuote(raise_exc=httpx.ConnectError("refused")), "Failed to call quote service: refused"),
        (FakeQuote(b"not-a-number"), "Failed to parse quote value as f64"),
        (FakeQuote(b" 8.99"), "Failed to parse quote value as f64"),
        (FakeQuote(b"\xff"), "Failed to parse quote service response as UTF-8"),
    ],
)
def test_get_quote_failure(fake, fragment):
    client, _ = make_client(quote=fake)
    before = sum(p.value for _, _, p in metric_points("demo.shipping.items_shipped"))
    resp = client.post("/get-quote", json={"items": [{"product_id": "A", "quantity": 2}]})
    assert resp.status_code == 500
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.text.startswith("Failed to get quote: status: 'Unknown error', self: \"")
    assert fragment in resp.text
    assert sum(p.value for _, _, p in metric_points("demo.shipping.items_shipped")) == before
    assert logs_named("shipping.quote.sent") == []


def test_quote_non_200_body_still_parsed():
    client, _ = make_client(quote=FakeQuote(b"8.99", status=500))
    assert client.post("/get-quote", json={"items": [{"product_id": "A", "quantity": 1}]}).status_code == 200


@pytest.mark.parametrize(
    "kwargs,status",
    [
        ({"json": {"address": None}}, 400),
        ({"json": {"items": [{"product_id": "A"}]}}, 400),
        ({"json": {"items": [{"product_id": "A", "quantity": -1}]}}, 400),
        ({"json": {"items": [{"product_id": "A", "quantity": 1.5}]}}, 400),
        ({"json": {"items": [], "address": {"country": "US"}}}, 400),
        ({"content": b"{bad", "headers": {"content-type": "application/json"}}, 400),
        ({"content": b'{"items":[]}', "headers": {"content-type": "text/plain"}}, 400),
        ({"content": b'{"items":[]}'}, 400),
    ],
)
@pytest.mark.parametrize("path", ["/get-quote", "/ship-order"])
def test_invalid_payload(path, kwargs, status):
    client, quote = make_client()
    resp = client.post(path, **kwargs)
    assert resp.status_code == status
    assert quote.requests == []


def ship(client, addr):
    body = {"items": []}
    if addr is not None:
        body["address"] = addr
    resp = client.post("/ship-order", json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert set(data) == {"tracking_id"}
    assert uuid.UUID(data["tracking_id"]).version == 4
    return data


def test_ship_order_no_address():
    flags = FakeFlags(10)
    ship(make_client(flags)[0], None)
    assert flags.calls == []


@pytest.mark.parametrize("country", ["US", "usa", " United States ", "united states of america"])
def test_ship_order_us_flag_on_not_delayed(country):
    flags = FakeFlags(10, "10sec")
    start = time.monotonic()
    ship(make_client(flags)[0], address(country))
    assert time.monotonic() - start < 1
    assert flags.calls == []


def test_ship_order_international_flag_off():
    flags = FakeFlags(0, "off")
    ship(make_client(flags)[0], address("FR"))
    assert flags.calls == [("intlShippingSlowdown", 0)]
    (evaluated,) = logs_named("shipping.feature_flag.evaluated")
    assert dict(evaluated.attributes) == {
        "feature_flag_key": "intlShippingSlowdown",
        "feature_flag_provider_name": "flagd",
        "feature_flag_variant": "off",
    }
    assert logs_named("shipping.international.slowdown") == []
    (created,) = logs_named("shipping.tracking_id.created")
    assert created.body == "Tracking ID Created"


def test_ship_order_international_flag_on():
    start = time.monotonic()
    data = ship(make_client(FakeFlags(1, None))[0], address("FR"))
    assert time.monotonic() - start >= 1
    (evaluated,) = logs_named("shipping.feature_flag.evaluated")
    assert evaluated.attributes["feature_flag_variant"] == "unknown"
    (slow,) = logs_named("shipping.international.slowdown")
    assert slow.attributes["delay_secs"] == 1
    assert slow.body == "Delaying international shipment due to intlShippingSlowdown feature flag"
    (created,) = logs_named("shipping.tracking_id.created")
    assert created.attributes["tracking_id"] == data["tracking_id"]


def test_ship_order_flag_error_falls_back_to_zero():
    start = time.monotonic()
    ship(make_client(FakeFlags(0, error="PROVIDER_NOT_READY: x"))[0], address("FR"))
    assert time.monotonic() - start < 1
    (failed,) = logs_named("shipping.feature_flag.evaluation_failed")
    assert failed.severity_text == "WARN"
    assert failed.attributes["error"] == "PROVIDER_NOT_READY: x"


def test_health():
    resp = make_client()[0].get("/health")
    assert resp.status_code == 200
    assert resp.content == b""


def test_log_scopes():
    ship(make_client(FakeFlags(0))[0], address("FR"))
    assert {l.instrumentation_scope.name for l in LOGS.get_finished_logs()} == {"shipping::shipping_service"}
