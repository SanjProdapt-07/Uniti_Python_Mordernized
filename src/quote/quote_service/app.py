# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import json
import time
from datetime import datetime
from urllib.parse import parse_qsl

from fastapi import FastAPI, Request, Response
from opentelemetry import metrics, trace
from opentelemetry._logs import SeverityNumber, get_logger
from opentelemetry.trace import SpanKind

from . import php

COST_PER_ITEM = 8.99

_counter = None


def _quote_counter():
    global _counter
    if _counter is None:
        _counter = metrics.get_meter("quotes").create_counter(
            "quotes", unit="quotes", description="number of quotes calculated"
        )
    return _counter


def parse_body(content_type: str | None, raw: bytes):
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    if media_type.endswith("+json"):
        media_type = "application/json"
    if media_type == "application/json":
        try:
            parsed = json.loads(raw)
        except ValueError:
            return None
        return parsed if isinstance(parsed, (dict, list)) else None
    if media_type == "application/x-www-form-urlencoded":
        return dict(parse_qsl(raw.decode("utf-8", "replace"), keep_blank_values=True))
    return None


def calculate_quote(body) -> float:
    quote = 0.0
    span = trace.get_tracer("manual-instrumentation").start_span(
        "calculate-quote", kind=SpanKind.INTERNAL
    )
    span.add_event("Calculating quote")
    try:
        if not isinstance(body, (dict, list)):
            return quote
        if not isinstance(body, dict) or "numberOfItems" not in body:
            raise ValueError("numberOfItems not provided")
        number_of_items = php.intval(body["numberOfItems"])
        quote = php.round2(COST_PER_ITEM * number_of_items)

        span.set_attribute("demo.shipping.quote.items_count", number_of_items)
        span.set_attribute("demo.shipping.quote.cost.total", quote)
        span.add_event("Quote calculated, returning its value")

        _quote_counter().add(1, {"number_of_items": number_of_items})
    except ValueError as exc:
        span.record_exception(exc)
    finally:
        span.end()
    return quote


def create_app() -> FastAPI:
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    logger = get_logger("slim-app")

    @app.middleware("http")
    async def access_log(request: Request, call_next):
        response = await call_next(request)
        size = response.headers.get("content-length", "0")
        print(
            f'[{datetime.now().astimezone().isoformat(sep=" ", timespec="seconds")}] '
            f'"{request.method} {request.url.path} HTTP/{request.scope.get("http_version", "1.1")}" '
            f"{response.status_code} {size} ",
            flush=True,
        )
        return response

    @app.post("/getquote")
    async def getquote(request: Request) -> Response:
        span = trace.get_current_span()
        span.add_event("Received get quote request, processing it")

        body = parse_body(request.headers.get("content-type"), await request.body())
        data = calculate_quote(body)

        span.add_event(
            "Quote processed, response sent back",
            {"demo.shipping.quote.cost.total": data},
        )
        logger.emit(
            timestamp=time.time_ns(),
            severity_number=SeverityNumber.INFO,
            severity_text="info",
            body="Calculated quote",
            attributes={"context": {"total": data}},
        )
        return Response(content=php.json_float(data), media_type="application/json")

    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except ImportError:
        return app
    FastAPIInstrumentor.instrument_app(app, exclude_spans=["receive", "send"])
    return app
