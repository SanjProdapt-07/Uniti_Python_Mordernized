# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import pytest
from conftest import LOGS, SPANS, metric_points
from fastapi.testclient import TestClient

from quote_service import php
from quote_service.app import create_app

client = TestClient(create_app())


def spans_by_name():
    return {s.name: s for s in SPANS.get_finished_spans()}


@pytest.mark.parametrize(
    "items,expected",
    [(1, "8.99"), (3, "26.97"), (4, "35.96"), (8, "71.92"), (0, "0.0"), (100, "899.0"), (-1, "-8.99")],
)
def test_getquote_values(items, expected):
    resp = client.post("/getquote", json={"numberOfItems": items})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/json"
    assert resp.text == expected


def test_getquote_telemetry():
    before = sum(p.value for _, _, p in metric_points("quotes") if p.attributes == {"number_of_items": 3})
    client.post("/getquote", json={"numberOfItems": 3})
    spans = spans_by_name()
    calc = spans["calculate-quote"]
    assert calc.instrumentation_scope.name == "manual-instrumentation"
    assert calc.kind.name == "INTERNAL"
    assert [e.name for e in calc.events] == ["Calculating quote", "Quote calculated, returning its value"]
    assert calc.attributes["demo.shipping.quote.items_count"] == 3
    assert calc.attributes["demo.shipping.quote.cost.total"] == 26.97

    server = spans["POST /getquote"]
    assert calc.parent.span_id == server.context.span_id
    assert [e.name for e in server.events] == [
        "Received get quote request, processing it",
        "Quote processed, response sent back",
    ]
    assert server.events[1].attributes["demo.shipping.quote.cost.total"] == 26.97
    assert set(spans) == {"calculate-quote", "POST /getquote"}

    points = metric_points("quotes")
    scope, metric, _ = points[0]
    assert scope == "quotes"
    assert metric.unit == "quotes"
    assert metric.description == "number of quotes calculated"
    after = sum(p.value for _, _, p in points if p.attributes == {"number_of_items": 3})
    assert after == before + 1

    (log,) = LOGS.get_finished_logs()
    assert log.instrumentation_scope.name == "slim-app"
    record = log.log_record
    assert record.body == "Calculated quote"
    assert record.severity_text == "info"
    assert record.severity_number.name == "INFO"
    assert dict(record.attributes["context"]) == {"total": 26.97}
    assert record.trace_id == server.context.trace_id


def test_missing_number_of_items_records_exception():
    resp = client.post("/getquote", json={"foo": 1})
    assert resp.text == "0.0"
    calc = spans_by_name()["calculate-quote"]
    names = [e.name for e in calc.events]
    assert names == ["Calculating quote", "exception"]
    assert calc.events[1].attributes["exception.message"] == "numberOfItems not provided"
    assert "demo.shipping.quote.items_count" not in calc.attributes


@pytest.mark.parametrize(
    "kwargs",
    [
        {"content": b"not json", "headers": {"content-type": "application/json"}},
        {"content": b"5", "headers": {"content-type": "application/json"}},
        {"content": b'{"numberOfItems":3}', "headers": {"content-type": "text/plain"}},
        {"content": b'{"numberOfItems":3}'},
    ],
)
def test_unparsed_body_returns_zero_without_exception(kwargs):
    resp = client.post("/getquote", **kwargs)
    assert resp.status_code == 200
    assert resp.text == "0.0"
    calc = spans_by_name()["calculate-quote"]
    assert [e.name for e in calc.events] == ["Calculating quote"]


def test_json_list_body_records_exception():
    resp = client.post("/getquote", json=[1, 2])
    assert resp.text == "0.0"
    assert [e.name for e in spans_by_name()["calculate-quote"].events] == ["Calculating quote", "exception"]


def test_form_body_and_vendor_json():
    assert client.post("/getquote", data={"numberOfItems": "2"}).text == "17.98"
    resp = client.post(
        "/getquote", content=b'{"numberOfItems":2}', headers={"content-type": "application/vnd.api+json; charset=utf-8"}
    )
    assert resp.text == "17.98"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("5", "44.95"), ("5abc", "44.95"), ("abc", "0.0"), (" 2", "17.98"), ("1e1", "89.9"),
        (2.9, "17.98"), (True, "8.99"), (None, "0.0"), ([], "0.0"), ([7], "8.99"), ({"a": 1}, "8.99"),
    ],
)
def test_php_intval_coercion(value, expected):
    assert client.post("/getquote", json={"numberOfItems": value}).text == expected


def test_php_helpers():
    assert php.intval("99999999999999999999") == 2**63 - 1
    assert php.intval(1e30) == 0
    assert php.round2(1.005) == 1.01
    assert php.round2(-1.005) == -1.01
    assert php.json_float(1e15) == "1.0e+15"
    assert php.json_float(1.2345e20) == "1.2345e+20"
    assert php.json_float(123456789012345.67) == "123456789012345.67"
    assert php.json_float(0.0001) == "0.0001"
    assert php.json_float(0.00001) == "1.0e-5"


def test_unknown_route_and_method():
    assert client.post("/nope").status_code == 404
    assert client.get("/getquote").status_code == 405
