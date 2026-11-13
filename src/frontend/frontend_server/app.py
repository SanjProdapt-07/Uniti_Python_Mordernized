# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import os
from collections.abc import Mapping
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from starlette.types import ASGIApp, Receive, Scope, Send

from . import api, pages
from .gateways import Backends

DEFAULT_STATIC_DIR = Path(__file__).resolve().parent.parent / "out"


class TrailingSlashRedirect:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            raw_path = scope.get("raw_path") or scope["path"].encode()
            path = raw_path.decode("latin-1")
            if path != "/" and path.endswith("/"):
                destination = path.rstrip("/") or "/"
                if scope.get("query_string"):
                    destination += "?" + scope["query_string"].decode("latin-1")
                body = destination.encode("latin-1")
                await send(
                    {
                        "type": "http.response.start",
                        "status": 308,
                        "headers": [
                            (b"location", body),
                            (b"refresh", b"0;url=" + body),
                            (b"content-length", str(len(body)).encode()),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def create_app(
    env: Mapping[str, str] | None = None,
    static_dir: Path | None = None,
    http_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    env = os.environ if env is None else env
    backends = Backends(env, http_transport)
    site = pages.StaticSite(static_dir or Path(env.get("FRONTEND_STATIC_DIR", DEFAULT_STATIC_DIR)), env)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await backends.start()
        try:
            yield
        finally:
            await backends.close()

    app = FastAPI(lifespan=lifespan, redirect_slashes=False, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.backends = backends
    app.state.site = site
    app.include_router(api.router)
    app.include_router(pages.router)
    app.add_middleware(TrailingSlashRedirect)
    if not getattr(app, "_is_instrumented_by_opentelemetry", False):
        FastAPIInstrumentor.instrument_app(app, exclude_spans=["receive", "send"])
    return app
