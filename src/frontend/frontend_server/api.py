# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import grpc
from fastapi import APIRouter
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from starlette.requests import Request
from starlette.responses import Response

from .gateways import Backends
from .logger import logger
from .nextcompat import (
    ApiError,
    field,
    parse_body,
    query_string,
    query_value,
    send,
    send_error,
    send_json,
)
from .protojson import to_js

PAYMENT_FAILURE_PREFIX = "failed to charge card:"

ALL_METHODS = ["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]

Handler = Callable[[Request, Any, Backends], Awaitable[Response]]


def _method_not_allowed(request: Request) -> Response:
    return send(request, 405, "")


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value if isinstance(value, str) else str(value)


def instrumented(handler: Handler) -> Callable[[Request], Awaitable[Response]]:
    async def endpoint(request: Request) -> Response:
        try:
            body = await parse_body(request)
        except ApiError as error:
            return send_error(error.status_code, error.message)

        span = trace.get_current_span()
        attributes = {"http.request.method": request.method, "url.path": request.url.path}
        status = 200
        try:
            response = await handler(request, body, request.app.state.backends)
            status = response.status_code
            logger.info("API request completed", extra={**attributes, "http.response.status_code": status})
            return response
        except Exception as error:
            span.record_exception(error)
            span.set_status(StatusCode.ERROR)
            status = 500
            logger.error(
                "API request failed",
                exc_info=error,
                extra={**attributes, "http.response.status_code": status},
            )
            return send_error(500, "Internal Server Error")
        finally:
            span.set_attribute("http.status_code", status)

    return endpoint


async def _product_cart_items(backends: Backends, items: list[tuple[str, int]], currency_code: str) -> list[dict]:
    products = await asyncio.gather(*(backends.get_product(pid, currency_code) for pid, _ in items))
    return [
        {"productId": pid, "quantity": quantity, "product": to_js(product)}
        for (pid, quantity), product in zip(items, products)
    ]


async def cart(request: Request, body: Any, backends: Backends) -> Response:
    match request.method:
        case "GET":
            session_id = query_string(request, "sessionId")
            currency_code = query_string(request, "currencyCode")
            result = await backends.get_cart(session_id)
            items = await _product_cart_items(
                backends, [(i.product_id, i.quantity) for i in result.items], currency_code
            )
            return send_json(request, 200, {"userId": result.user_id, "items": items})
        case "POST":
            user_id = _as_str(field(body, "userId"))
            await backends.add_item(user_id, field(body, "item"))
            result = await backends.get_cart(user_id)
            return send_json(request, 200, to_js(result))
        case "DELETE":
            await backends.empty_cart(_as_str(field(body, "userId")))
            return send(request, 204, "")
    return _method_not_allowed(request)


async def checkout(request: Request, body: Any, backends: Backends) -> Response:
    if request.method != "POST":
        return _method_not_allowed(request)

    currency_code = query_string(request, "currencyCode")
    try:
        response = await backends.place_order(body)
    except Exception as error:
        span = trace.get_current_span()
        span.record_exception(error)
        span.set_status(StatusCode.ERROR)

        details = (error.details() if isinstance(error, grpc.aio.AioRpcError) else str(error)) or ""

        if details.startswith(PAYMENT_FAILURE_PREFIX):
            if "code = Unavailable" not in details and "code = Internal" not in details:
                logger.info("Checkout payment declined", extra={"details": details})
                return send_json(
                    request,
                    422,
                    {
                        "error": "Your payment could not be processed. Please check your card details and try again.",
                        "code": "PAYMENT_FAILED",
                    },
                )

        logger.error("Checkout failed to place order", exc_info=error)
        return send_json(request, 500, {"error": "Failed to place order."})

    order: dict[str, Any] = {}
    order_items: list[Any] = []
    if response.HasField("order"):
        order = to_js(response.order)
        order.pop("items")
        order_items = list(response.order.items)

    products = await asyncio.gather(
        *(
            backends.get_product(oi.item.product_id if oi.HasField("item") else "", currency_code)
            for oi in order_items
        )
    )
    product_list = []
    for oi, product in zip(order_items, products):
        entry: dict[str, Any] = {}
        if oi.HasField("cost"):
            entry["cost"] = to_js(oi.cost)
        entry["item"] = {
            "productId": oi.item.product_id if oi.HasField("item") else "",
            "quantity": oi.item.quantity if oi.HasField("item") else 0,
            "product": to_js(product),
        }
        product_list.append(entry)

    extra = {"demo.order.id": response.order.order_id} if response.HasField("order") else {}
    logger.info("Order placed successfully", extra=extra)
    return send_json(request, 200, {**order, "items": product_list})


async def currency(request: Request, body: Any, backends: Backends) -> Response:
    if request.method != "GET":
        return _method_not_allowed(request)
    response = await backends.get_supported_currencies()
    return send_json(request, 200, list(response.currency_codes))


async def data(request: Request, body: Any, backends: Backends) -> Response:
    if request.method != "GET":
        return _method_not_allowed(request)
    context_keys = query_value(request, "contextKeys")
    if context_keys is None:
        keys: list[str] = []
    elif isinstance(context_keys, list):
        keys = context_keys
    else:
        keys = context_keys.split(",")
    response = await backends.list_ads(keys)
    return send_json(request, 200, [to_js(ad) for ad in response.ads])


async def recommendations(request: Request, body: Any, backends: Backends) -> Response:
    if request.method != "GET":
        return _method_not_allowed(request)
    product_ids = query_value(request, "productIds")
    if product_ids is None:
        ids: list[str] = []
    elif isinstance(product_ids, list):
        ids = product_ids
    else:
        ids = list(product_ids)
    session_id = query_string(request, "sessionId")
    currency_code = query_string(request, "currencyCode")
    response = await backends.list_recommendations(session_id, ids)
    products = await asyncio.gather(
        *(backends.get_product(pid, currency_code) for pid in list(response.product_ids)[:4])
    )
    return send_json(request, 200, [to_js(p) for p in products])


async def shipping(request: Request, body: Any, backends: Backends) -> Response:
    if request.method != "GET":
        return _method_not_allowed(request)
    item_list = json.loads(query_string(request, "itemList"))
    currency_code = query_string(request, "currencyCode", "USD")
    address = json.loads(query_string(request, "address"))
    if not isinstance(item_list, list):
        raise TypeError("itemList.map is not a function")
    if address is None:
        raise TypeError("Cannot read properties of null (reading 'streetAddress')")
    cost_usd = await backends.get_shipping_cost(item_list, address)
    cost = await backends.convert(cost_usd, currency_code)
    return send_json(request, 200, to_js(cost))


async def products(request: Request, body: Any, backends: Backends) -> Response:
    if request.method != "GET":
        return _method_not_allowed(request)
    product_list = await backends.list_products(query_string(request, "currencyCode"))
    return send_json(request, 200, [to_js(p) for p in product_list])


async def product(request: Request, body: Any, backends: Backends) -> Response:
    if request.method != "GET":
        return _method_not_allowed(request)
    result = await backends.get_product(request.path_params["productId"], query_string(request, "currencyCode"))
    return send_json(request, 200, to_js(result))


router = APIRouter()
for path, handler in (
    ("/api/cart", cart),
    ("/api/checkout", checkout),
    ("/api/currency", currency),
    ("/api/data", data),
    ("/api/recommendations", recommendations),
    ("/api/shipping", shipping),
    ("/api/products", products),
    ("/api/products/{productId}", product),
):
    router.add_api_route(path, instrumented(handler), methods=ALL_METHODS, include_in_schema=False)
