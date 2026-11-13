# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
import uuid

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ValidationError

from .flags import FlagEvaluator
from .logs import emit
from .models import GetQuoteRequest, GetQuoteResponse, Money, ShipOrderRequest, ShipOrderResponse
from .quote import QuoteClient, QuoteError, create_quote_from_count

TARGET = "shipping::shipping_service"
NANOS_MULTIPLE = 10_000_000
JSON_LIMIT = 2_097_152
US_NAMES = {"US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA"}


class JsonPayloadError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message


async def parse_json[M: BaseModel](request: Request, model: type[M]) -> M:
    content_type = request.headers.get("content-type", "")
    media = content_type.split(";", 1)[0].strip().lower()
    if not (media == "application/json" or media.endswith("+json")):
        raise JsonPayloadError(400, "Content type error")
    body = await request.body()
    if len(body) > JSON_LIMIT:
        raise JsonPayloadError(413, f"JSON payload ({len(body)} bytes) is larger than allowed (limit: {JSON_LIMIT} bytes).")
    try:
        return model.model_validate(json.loads(body))
    except (ValueError, ValidationError) as err:
        raise JsonPayloadError(400, f"Json deserialize error: {err}") from err


def is_outside_us(request: ShipOrderRequest) -> bool:
    if request.address is None:
        return False
    return request.address.country.upper().strip() not in US_NAMES


def create_app(flags: FlagEvaluator, quote_client: QuoteClient) -> FastAPI:
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    @app.exception_handler(JsonPayloadError)
    async def json_payload_error(_: Request, exc: JsonPayloadError) -> Response:
        return PlainTextResponse(exc.message, status_code=exc.status)

    @app.post("/get-quote")
    async def get_quote(request: Request) -> Response:
        req = await parse_json(request, GetQuoteRequest)
        item_count = sum(item.quantity for item in req.items) % 2**32
        try:
            quote = await create_quote_from_count(quote_client, item_count)
        except QuoteError as err:
            return PlainTextResponse(f"Failed to get quote: {err}", status_code=500)

        reply = GetQuoteResponse(
            cost_usd=Money(currency_code="USD", units=quote.dollars, nanos=quote.cents * NANOS_MULTIPLE)
        )
        emit(TARGET, "INFO", "Sending Quote", "shipping.quote.sent", dollars=quote.dollars, cents=quote.cents)
        return JSONResponse(reply.model_dump())

    @app.post("/ship-order")
    async def ship_order(request: Request) -> Response:
        req = await parse_json(request, ShipOrderRequest)
        slowdown_secs = 0
        if is_outside_us(req):
            res = flags.resolve_int("intlShippingSlowdown", 0)
            if res.error is None:
                emit(
                    TARGET,
                    "INFO",
                    event_name="shipping.feature_flag.evaluated",
                    feature_flag_key="intlShippingSlowdown",
                    feature_flag_provider_name="flagd",
                    feature_flag_variant=res.variant or "unknown",
                )
                slowdown_secs = res.value
            else:
                emit(TARGET, "WARN", event_name="shipping.feature_flag.evaluation_failed", error=res.error)

        if slowdown_secs > 0:
            emit(
                TARGET,
                "INFO",
                "Delaying international shipment due to intlShippingSlowdown feature flag",
                "shipping.international.slowdown",
                delay_secs=slowdown_secs,
            )
            await asyncio.sleep(slowdown_secs)

        tracking_id = str(uuid.uuid4())
        emit(TARGET, "INFO", "Tracking ID Created", "shipping.tracking_id.created", tracking_id=tracking_id)
        return JSONResponse(ShipOrderResponse(tracking_id=tracking_id).model_dump())

    @app.get("/health")
    async def health() -> Response:
        return Response(status_code=200)

    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except ImportError:
        return app
    FastAPIInstrumentor.instrument_app(app, exclude_spans=["receive", "send"])
    return app
