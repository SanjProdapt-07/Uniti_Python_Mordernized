# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import copy
import pytest
from conftest import LOGS, SPANS, metric_points
from fastapi.testclient import TestClient

from email_service.app import create_app
from email_service.mailer import TestMailer

EXPECTED_ITEMS = """\
        
          <tr>
            <td>OLJCESPC7Z</td>
            <td>2</td>
            <td>101.960000000 USD</td>
          </tr>
        
          <tr>
            <td>66VCHSJNUP</td>
            <td>1</td>
            <td>349.950000000 USD</td>
          </tr>
        
"""

ORDER = {
    "order_id": "505b5c4c-0e3b-11ef-a6f6-0242ac120007",
    "shipping_tracking_id": "e3b0c442-98fc-1c14-9afb-f4c8996fb924",
    "shipping_cost": {"currency_code": "USD", "units": 8, "nanos": 990000000},
    "shipping_address": {
        "street_address": "1600 Amphitheatre Parkway",
        "city": "Mountain View",
        "state": "CA",
        "country": "United States",
        "zip_code": "94043",
    },
    "items": [
        {"item": {"product_id": "OLJCESPC7Z", "quantity": 2}, "cost": {"currency_code": "USD", "units": 101, "nanos": 960000000}},
        {"item": {"product_id": "66VCHSJNUP", "quantity": 1}, "cost": {"currency_code": "USD", "units": 349, "nanos": 950000000}},
    ],
}


class FakeFlags:
    def __init__(self, value=0):
        self.value = value
        self.calls = []

    def number(self, key, default):
        self.calls.append((key, default))
        return self.value


def make(value=0):
    flags = FakeFlags(value)
    mailer = TestMailer()
    return TestClient(create_app(flags, mailer)), flags, mailer


def payload(**overrides):
    order = copy.deepcopy(ORDER)
    order.update(overrides)
    return {"email": "someone@example.com", "order": order}


def confirmations():
    return sum(p.value for _, _, p in metric_points("demo.notification.confirmations"))


EXPECTED_HEAD = """<p>505b5c4c-0e3b-11ef-a6f6-0242ac120007</p>
    <h3>Shipping</h3>
    <p>e3b0c442-98fc-1c14-9afb-f4c8996fb924</p>
    <p>8.990000000 USD</p>
    <p>, , Mountain View, United States 94043</p>"""


def test_send_order_confirmation_success():
    client, flags, mailer = make(0)
    before = confirmations()
    resp = client.post("/send_order_confirmation", json=payload())
    assert resp.status_code == 200
    assert resp.content == b""
    assert resp.headers["content-type"] == "text/html;charset=utf-8"
    assert flags.calls == [("emailMemoryLeak", 0)]
    assert mailer.deliveries == []
    assert confirmations() == before + 1

    spans = {s.name: s for s in SPANS.get_finished_spans()}
    server = spans["POST /send_order_confirmation"]
    send = spans["send_email"]
    render = spans["sinatra.render_template"]
    assert server.attributes["demo.order.id"] == ORDER["order_id"]
    assert send.attributes["demo.order.id"] == ORDER["order_id"]
    assert send.instrumentation_scope.name == "email"
    assert send.parent.span_id == server.context.span_id
    assert render.parent.span_id == send.context.span_id
    assert render.attributes["sinatra.template_name"] == "confirmation"

    (log,) = LOGS.get_finished_logs()
    assert log.instrumentation_scope.name == "email"
    record = log.log_record
    assert record.body == "Order confirmation email sent"
    assert record.event_name == "email.confirmation_sent"
    assert record.severity_text == "INFO"
    assert dict(record.attributes) == {"demo.order.id": ORDER["order_id"]}
    assert record.span_id == send.context.span_id

    (_, metric, point) = metric_points("demo.notification.confirmations")[0]
    assert metric.unit == "1"
    assert metric.description == "Counts the number of order confirmation emails sent"
    assert dict(point.attributes) == {}


def test_rendered_body_matches_erb_output():
    client, _, mailer = make(1)
    client.post("/send_order_confirmation", json=payload())
    (message,) = mailer.deliveries
    assert message.to == "someone@example.com"
    assert message.from_ == "noreply@example.com"
    assert message.subject == "Your confirmation email"
    body = message.body
    assert EXPECTED_HEAD in body
    assert EXPECTED_ITEMS in body
    assert body.startswith("<!DOCTYPE html>\n<!--\n Copyright 2020 Google LLC")
    assert body.endswith("  </body>\n</html>\n")


def test_memory_leak_multiplier():
    client, _, mailer = make(10)
    client.post("/send_order_confirmation", json=payload())
    client.post("/send_order_confirmation", json=payload())
    assert len(mailer.deliveries) == 2
    body = mailer.deliveries[0].body
    content_len = len(body.rstrip(" "))
    assert len(body) == content_len * 10
    assert body.endswith(" " * (content_len * 9))


def test_multiplier_one_accumulates_without_padding():
    client, _, mailer = make(1)
    client.post("/send_order_confirmation", json=payload())
    assert len(mailer.deliveries) == 1
    assert not mailer.deliveries[0].body.endswith(" ")


def test_omitted_fields_render_empty():
    client, _, mailer = make(1)
    order = payload()
    order["order"]["shipping_cost"] = {"currency_code": "USD", "units": 8}
    resp = client.post("/send_order_confirmation", json=order)
    assert resp.status_code == 200
    assert "<p>8. USD</p>" in mailer.deliveries[0].body


@pytest.mark.parametrize(
    "body,counted",
    [
        (b"not json", False),
        (b"[]", False),
        (b'{"email":"a@b.c"}', False),
        (None, True),
    ],
)
def test_failures_return_500(body, counted):
    client, _, mailer = make(0)
    before = confirmations()
    if body is None:
        resp = client.post("/send_order_confirmation", json=payload(shipping_cost=None))
    else:
        resp = client.post("/send_order_confirmation", content=body)
    assert resp.status_code == 500
    assert resp.content == b""
    assert confirmations() == before + (1 if counted else 0)
    server = next(s for s in SPANS.get_finished_spans() if s.name == "POST /send_order_confirmation")
    assert any(e.name == "exception" for e in server.events)
    assert server.status.status_code.name == "ERROR"
    assert LOGS.get_finished_logs() == ()


def test_missing_items_and_recipient_fail():
    client, _, _ = make(0)
    assert client.post("/send_order_confirmation", json=payload(items=None)).status_code == 500
    assert client.post("/send_order_confirmation", json=payload(items="abc")).status_code == 500
    no_email = payload()
    del no_email["email"]
    assert client.post("/send_order_confirmation", json=no_email).status_code == 500
    send = next(s for s in SPANS.get_finished_spans() if s.name == "send_email")
    assert send.status.status_code.name == "ERROR"


def test_missing_order_id_still_sends():
    client, _, _ = make(0)
    order = payload()
    del order["order"]["order_id"]
    assert client.post("/send_order_confirmation", json=order).status_code == 200
    server = next(s for s in SPANS.get_finished_spans() if s.name == "POST /send_order_confirmation")
    assert "demo.order.id" not in server.attributes
