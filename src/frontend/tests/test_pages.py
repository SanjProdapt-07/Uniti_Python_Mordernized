# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import pytest

from frontend_server.pages import PAGE_CACHE_CONTROL, StaticSite

from conftest import ENV

REAL_ENV = """
        window.ENV = {
          NEXT_PUBLIC_PLATFORM: 'local',
          NEXT_PUBLIC_OTEL_SERVICE_NAME: 'frontend-web',
          NEXT_PUBLIC_OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: 'http://localhost:8080/otlp-http/v1/traces',
          IS_SYNTHETIC_REQUEST: 'false',
        };"""

SYNTHETIC_ENV = """
        window.ENV = {
          NEXT_PUBLIC_PLATFORM: 'local',
          NEXT_PUBLIC_OTEL_SERVICE_NAME: 'frontend-web',
          NEXT_PUBLIC_OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: 'http://otel-collector:4318/v1/traces',
          IS_SYNTHETIC_REQUEST: 'true',
        };"""


@pytest.mark.parametrize(
    ("path", "title"),
    [
        ("/", "Home"),
        ("/cart", "Cart"),
        ("/product/OLJCESPC7Z", "Product"),
        ("/cart/checkout/abc-123?order=%7B%7D", "Checkout"),
    ],
)
def test_pages_serve_exported_shell_with_env(client, path, title):
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.headers["cache-control"] == PAGE_CACHE_CONTROL
    assert f"<title>Otel Demo - {title}</title>" in response.text
    assert f"<script>{REAL_ENV}</script>" in response.text
    assert "__FRONTEND_ENV__" not in response.text


def test_synthetic_request_baggage_switches_traces_endpoint(client):
    response = client.get("/cart", headers={"baggage": "session.id=abc, synthetic_request=true"})
    assert f"<script>{SYNTHETIC_ENV}</script>" in response.text


def test_other_baggage_values_are_not_synthetic(client):
    response = client.get("/", headers={"baggage": "synthetic_request=false"})
    assert f"<script>{REAL_ENV}</script>" in response.text


def test_unset_env_renders_undefined_like_template_literals(static_dir):
    site = StaticSite(static_dir, {})
    body = site.rendered[("/", True)][0].decode()
    assert "NEXT_PUBLIC_PLATFORM: 'undefined'" in body
    assert "NEXT_PUBLIC_OTEL_SERVICE_NAME: 'undefined'" in body
    assert "'http://undefined:4318/v1/traces'" in body


def test_missing_placeholder_fails_fast(static_dir):
    (static_dir / "cart.html").write_text("<html></html>")
    with pytest.raises(RuntimeError):
        StaticSite(static_dir, ENV)


def test_unknown_pages_and_api_routes_render_404(client):
    for path in ("/nope", "/api/nope", "/product/a/b", "/cart.html", "/404"):
        response = client.get(path)
        assert response.status_code == 404, path
        assert "Page Not Found" in response.text
    assert client.post("/api/nope").status_code == 404


def test_500_page(client):
    response = client.get("/500")
    assert response.status_code == 500
    assert "Server Error" in response.text


def test_page_trailing_slash_redirects(client):
    response = client.get("/cart/")
    assert response.status_code == 308
    assert response.headers["location"] == "/cart"


def test_static_assets(client):
    chunk = client.get("/_next/static/chunks/main.js")
    assert chunk.status_code == 200
    assert chunk.headers["cache-control"] == "public, max-age=31536000, immutable"
    icon = client.get("/icons/Cart.svg?w=16&q=75")
    assert icon.status_code == 200
    assert icon.headers["cache-control"] == "public, max-age=0"
    assert client.get("/favicon.ico").content == b"\x00\x00\x01\x00"
    assert client.get("/_next/static/chunks/missing.js").status_code == 404
    assert client.get("/../../etc/passwd").status_code == 404


def test_page_etag_revalidation(client):
    first = client.get("/")
    second = client.get("/", headers={"if-none-match": first.headers["etag"]})
    assert second.status_code == 304
