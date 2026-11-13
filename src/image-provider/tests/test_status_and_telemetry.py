# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import re
import socket
import threading
import time

import httpx
import pytest
from opentelemetry.trace import SpanKind, StatusCode

from conftest import STATIC, call
from image_provider.__main__ import build_server, listen_sockets
from image_provider.app import ImageProvider
from image_provider.pages import error_page
from image_provider.status import Stats
from image_provider.telemetry import collector_endpoint

# Matches the four-line nginx stub_status body and captures its eight counters.
STUB_STATUS = re.compile(
    rb"\AActive connections: (\d+) \n"
    rb"server accepts handled requests\n"
    rb" (\d+) (\d+) (\d+) \n"
    rb"Reading: (\d+) Writing: (\d+) Waiting: (\d+) \n\Z"
)


@pytest.mark.parametrize("path", [b"/status", b"/status/", b"/status/anything", b"/statusx"])
def test_status_location_is_a_prefix_match(app, path):
    response = call(app, "GET", path)
    assert response.status == 200
    assert response.headers["content-type"] == "text/plain"
    assert STUB_STATUS.match(response.body)


def test_status_counts_requests(app):
    call(app, "GET", b"/Banner.png")
    call(app, "GET", b"/missing")
    body = call(app, "GET", b"/status").body
    match = STUB_STATUS.match(body)
    assert int(match.group(4)) == 3
    assert int(match.group(6)) == 1


def test_status_head_and_methods(app):
    head = call(app, "HEAD", b"/status")
    assert head.status == 200
    assert head.body == b""
    for method in ("POST", "PUT", "DELETE"):
        response = call(app, method, b"/status")
        assert response.status == 405
        assert response.body == error_page(405)


def test_span_for_image_request(app, exporter):
    trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
    parent = "00f067aa0ba902b7"
    call(
        app,
        "GET",
        b"/products/RoofBinoculars.jpg",
        {"traceparent": f"00-{trace_id}-{parent}-01", "user-agent": "pytest"},
        query=b"w=640&q=75",
    )
    (span,) = exporter.get_finished_spans()
    assert span.name == "image-provider"
    assert span.kind == SpanKind.SERVER
    assert format(span.context.trace_id, "032x") == trace_id
    assert format(span.parent.span_id, "016x") == parent
    size = (STATIC / "products/RoofBinoculars.jpg").stat().st_size
    assert dict(span.attributes) == {
        "http.method": "GET",
        "http.target": "/products/RoofBinoculars.jpg?w=640&q=75",
        "http.scheme": "http",
        "http.flavor": "1.1",
        "http.user_agent": "pytest",
        "http.response_content_length": size,
        "http.status_code": 200,
        "net.host.name": "image-provider",
        "net.host.port": 8081,
        "net.sock.peer.addr": "10.0.0.7",
        "net.sock.peer.port": 51234,
    }
    assert span.status.status_code == StatusCode.UNSET


def test_span_for_status_has_route(app, exporter):
    call(app, "GET", b"/status")
    (span,) = exporter.get_finished_spans()
    assert span.attributes["http.route"] == "/status"


def test_span_for_404_and_head(app, exporter):
    call(app, "HEAD", b"/nope")
    (span,) = exporter.get_finished_spans()
    assert span.attributes["http.status_code"] == 404
    assert "http.response_content_length" not in span.attributes
    assert "http.route" not in span.attributes


def test_internal_error_returns_500_and_marks_span(app, exporter, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(app.static, "handle", boom)
    response = call(app, "GET", b"/Banner.png")
    assert response.status == 500
    assert response.body == error_page(500)
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes["http.status_code"] == 500


def test_access_log_line(provider, capsys):
    app = ImageProvider(STATIC, Stats(), tracer_provider=provider)
    call(app, "GET", b"/Banner.png", {"user-agent": "ua", "referer": "http://x/"})
    line = capsys.readouterr().out.strip()
    size = (STATIC / "Banner.png").stat().st_size
    # Matches the nginx "combined" log format.
    assert re.fullmatch(
        rf'10\.0\.0\.7 - - \[[^\]]+\] "GET /Banner\.png HTTP/1\.1" 200 {size} "http://x/" "ua"', line
    )


def test_collector_endpoint(monkeypatch):
    for name in ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OTEL_COLLECTOR_HOST", "otel-collector")
    monkeypatch.setenv("OTEL_COLLECTOR_PORT_GRPC", "4317")
    assert collector_endpoint() == "http://otel-collector:4317"
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://elsewhere:4317")
    assert collector_endpoint() is None


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def live_server(provider):
    app = ImageProvider(STATIC, Stats(), tracer_provider=provider, access_log=False)
    port = _free_port()
    server = build_server(app)
    sockets = listen_sockets(port)
    thread = threading.Thread(target=server.run, kwargs={"sockets": sockets}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started
    yield port, sockets
    server.should_exit = True
    thread.join(timeout=10)


def test_live_server_ipv4_and_stub_status_counters(live_server):
    port, _ = live_server
    with httpx.Client(base_url=f"http://127.0.0.1:{port}") as client:
        assert client.get("/Banner.png").status_code == 200
        assert client.get("/products/RedFlashlight.jpg").status_code == 200
        response = client.get("/status")
    assert response.status_code == 200
    assert "server" not in response.headers
    assert "date" in response.headers
    active, accepts, handled, requests, reading, writing, waiting = map(int, STUB_STATUS.match(response.content).groups())
    assert (active, accepts, handled, requests, writing) == (1, 1, 1, 3, 1)
    assert waiting == 0
    with httpx.Client(base_url=f"http://127.0.0.1:{port}") as client:
        second = STUB_STATUS.match(client.get("/status").content).groups()
    assert int(second[1]) == 2
    assert int(second[3]) == 4


def test_live_server_ipv6_localhost(live_server):
    port, sockets = live_server
    if not any(sock.family == socket.AF_INET6 for sock in sockets):
        pytest.skip("IPv6 not available")
    response = httpx.head(f"http://[::1]:{port}/status")
    assert response.status_code == 200


def test_live_server_directory_redirect(live_server):
    port, _ = live_server
    response = httpx.get(f"http://127.0.0.1:{port}/products", headers={"host": "image-provider:8081"})
    assert response.status_code == 301
    assert response.headers["location"] == f"http://image-provider:{port}/products/"
