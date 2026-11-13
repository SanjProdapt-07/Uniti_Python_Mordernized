# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

from collections.abc import Mapping
from pathlib import Path

from fastapi import APIRouter
from opentelemetry import baggage
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from starlette.requests import Request
from starlette.responses import FileResponse, Response

from .nextcompat import generate_etag, is_fresh

ENV_PLACEHOLDER = "window.ENV={};/*__FRONTEND_ENV__*/"
PAGE_CACHE_CONTROL = "private, no-cache, no-store, max-age=0, must-revalidate"
IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
PUBLIC_CACHE_CONTROL = "public, max-age=0"
HTML_CONTENT_TYPE = "text/html; charset=utf-8"

PAGES = {
    "/": "index",
    "/cart": "cart",
    "/product/[productId]": "product/[productId]",
    "/cart/checkout/[orderId]": "cart/checkout/[orderId]",
    "/404": "404",
    "/500": "500",
}


def env_script(env: Mapping[str, str], is_synthetic_request: bool) -> str:
    def value(name: str) -> str:
        return env.get(name, "undefined")

    otlp_traces_endpoint = (
        f"http://{value('OTEL_COLLECTOR_HOST')}:4318/v1/traces"
        if is_synthetic_request
        else value("PUBLIC_OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    )
    return f"""
        window.ENV = {{
          NEXT_PUBLIC_PLATFORM: '{value('ENV_PLATFORM')}',
          NEXT_PUBLIC_OTEL_SERVICE_NAME: '{value('WEB_OTEL_SERVICE_NAME')}',
          NEXT_PUBLIC_OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: '{otlp_traces_endpoint}',
          IS_SYNTHETIC_REQUEST: '{'true' if is_synthetic_request else 'false'}',
        }};"""


def is_synthetic_request(request: Request) -> bool:
    value = baggage.get_baggage("synthetic_request")
    if value is None:
        value = baggage.get_baggage("synthetic_request", W3CBaggagePropagator().extract(dict(request.headers)))
    return value == "true"


class StaticSite:
    def __init__(self, root: Path, env: Mapping[str, str]):
        self.root = root.resolve()
        self.rendered: dict[tuple[str, bool], tuple[bytes, str]] = {}
        for page, name in PAGES.items():
            html = self._read_page(name)
            if ENV_PLACEHOLDER not in html:
                raise RuntimeError(f"{name}.html does not contain the window.ENV placeholder")
            for synthetic in (False, True):
                body = html.replace(ENV_PLACEHOLDER, env_script(env, synthetic), 1)
                self.rendered[(page, synthetic)] = (body.encode(), generate_etag(body))

    def _read_page(self, name: str) -> str:
        for candidate in (self.root / f"{name}.html", self.root / name / "index.html"):
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8")
        raise RuntimeError(f"exported page {name}.html not found in {self.root}")

    def page(self, request: Request, page: str, status: int = 200) -> Response:
        body, etag = self.rendered[(page, is_synthetic_request(request))]
        headers = {"Cache-Control": PAGE_CACHE_CONTROL, "ETag": etag}
        if status == 200 and is_fresh(request, etag):
            return Response(status_code=304, headers=headers)
        headers["Content-Type"] = HTML_CONTENT_TYPE
        return Response(content=body, status_code=status, headers=headers)

    def file(self, path: str) -> Path | None:
        if not path or path.endswith(".html"):
            return None
        candidate = (self.root / path).resolve()
        if not candidate.is_relative_to(self.root) or not candidate.is_file():
            return None
        return candidate


router = APIRouter()


def _site(request: Request) -> StaticSite:
    return request.app.state.site


@router.get("/", include_in_schema=False)
@router.head("/", include_in_schema=False)
async def index(request: Request) -> Response:
    return _site(request).page(request, "/")


@router.get("/cart", include_in_schema=False)
@router.head("/cart", include_in_schema=False)
async def cart(request: Request) -> Response:
    return _site(request).page(request, "/cart")


@router.get("/product/{productId}", include_in_schema=False)
@router.head("/product/{productId}", include_in_schema=False)
async def product(request: Request) -> Response:
    return _site(request).page(request, "/product/[productId]")


@router.get("/cart/checkout/{orderId}", include_in_schema=False)
@router.head("/cart/checkout/{orderId}", include_in_schema=False)
async def checkout(request: Request) -> Response:
    return _site(request).page(request, "/cart/checkout/[orderId]")


@router.get("/500", include_in_schema=False)
@router.head("/500", include_in_schema=False)
async def server_error(request: Request) -> Response:
    return _site(request).page(request, "/500", 500)


@router.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"], include_in_schema=False)
async def static(request: Request, path: str) -> Response:
    site = _site(request)
    file = site.file(path) if request.method in ("GET", "HEAD") else None
    if file is None:
        return site.page(request, "/404", 404)
    cache_control = IMMUTABLE_CACHE_CONTROL if path.startswith("_next/static/") else PUBLIC_CACHE_CONTROL
    return FileResponse(file, headers={"Cache-Control": cache_control})
