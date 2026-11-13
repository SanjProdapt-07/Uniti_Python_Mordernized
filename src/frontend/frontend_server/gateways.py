# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
import os
from collections.abc import Mapping
from typing import Any

import grpc
import httpx

import demo_pb2
import demo_pb2_grpc

from .protojson import from_js

DEFAULT_CURRENCY_CODE = "USD"


class ShippingHttpError(Exception):
    pass


class Backends:
    def __init__(self, env: Mapping[str, str] | None = None, http_transport: httpx.AsyncBaseTransport | None = None):
        env = os.environ if env is None else env
        self.addresses = {
            name: env.get(name, "")
            for name in (
                "AD_ADDR",
                "CART_ADDR",
                "CHECKOUT_ADDR",
                "CURRENCY_ADDR",
                "PRODUCT_CATALOG_ADDR",
                "RECOMMENDATION_ADDR",
            )
        }
        self.shipping_addr = env.get("SHIPPING_ADDR", "")
        self._http_transport = http_transport
        self._channels: list[grpc.aio.Channel] = []
        self.http: httpx.AsyncClient | None = None

    def _channel(self, name: str) -> grpc.aio.Channel:
        channel = grpc.aio.insecure_channel(self.addresses[name])
        self._channels.append(channel)
        return channel

    async def start(self) -> None:
        self.ad = demo_pb2_grpc.AdServiceStub(self._channel("AD_ADDR"))
        self.cart = demo_pb2_grpc.CartServiceStub(self._channel("CART_ADDR"))
        self.checkout = demo_pb2_grpc.CheckoutServiceStub(self._channel("CHECKOUT_ADDR"))
        self.currency = demo_pb2_grpc.CurrencyServiceStub(self._channel("CURRENCY_ADDR"))
        self.product_catalog = demo_pb2_grpc.ProductCatalogServiceStub(self._channel("PRODUCT_CATALOG_ADDR"))
        self.recommendation = demo_pb2_grpc.RecommendationServiceStub(self._channel("RECOMMENDATION_ADDR"))
        self.http = httpx.AsyncClient(transport=self._http_transport, timeout=None)

    async def close(self) -> None:
        for channel in self._channels:
            await channel.close()
        self._channels.clear()
        if self.http is not None:
            await self.http.aclose()

    async def list_ads(self, context_keys: list[str]) -> demo_pb2.AdResponse:
        return await self.ad.GetAds(demo_pb2.AdRequest(context_keys=context_keys))

    async def get_cart(self, user_id: str) -> demo_pb2.Cart:
        return await self.cart.GetCart(demo_pb2.GetCartRequest(user_id=user_id))

    async def add_item(self, user_id: str, item: Any) -> None:
        request = demo_pb2.AddItemRequest(user_id=user_id)
        if item is not None:
            request.item.CopyFrom(from_js(demo_pb2.CartItem, item))
        await self.cart.AddItem(request)

    async def empty_cart(self, user_id: str) -> None:
        await self.cart.EmptyCart(demo_pb2.EmptyCartRequest(user_id=user_id))

    async def place_order(self, order: Any) -> demo_pb2.PlaceOrderResponse:
        return await self.checkout.PlaceOrder(from_js(demo_pb2.PlaceOrderRequest, order))

    async def convert(self, money: demo_pb2.Money | None, to_code: str) -> demo_pb2.Money:
        request = demo_pb2.CurrencyConversionRequest(to_code=to_code)
        if money is not None:
            getattr(request, "from").CopyFrom(money)
        return await self.currency.Convert(request)

    async def get_supported_currencies(self) -> demo_pb2.GetSupportedCurrenciesResponse:
        return await self.currency.GetSupportedCurrencies(demo_pb2.Empty())

    async def list_recommendations(self, user_id: str, product_ids: list[str]) -> demo_pb2.ListRecommendationsResponse:
        return await self.recommendation.ListRecommendations(
            demo_pb2.ListRecommendationsRequest(user_id=user_id, product_ids=product_ids)
        )

    async def get_shipping_cost(self, item_list: Any, address: Any) -> demo_pb2.Money | None:
        items = [_drop_none({"product_id": _get(i, "productId"), "quantity": _get(i, "quantity")}) for i in item_list]
        address_body = _drop_none(
            {
                "street_address": _get(address, "streetAddress"),
                "city": _get(address, "city"),
                "state": _get(address, "state"),
                "country": _get(address, "country"),
                "zip_code": _get(address, "zipCode"),
            }
        )
        body = json.dumps({"items": items, "address": address_body}, ensure_ascii=False, separators=(",", ":"))
        assert self.http is not None
        response = await self.http.post(
            f"{self.shipping_addr}/get-quote",
            content=body.encode(),
            headers={"Content-Type": "application/json"},
        )
        if not response.is_success:
            raise ShippingHttpError(f"HTTP error: {response.status_code} {response.reason_phrase} - {response.text}")
        cost = response.json().get("cost_usd")
        if not cost:
            return None
        return from_js(
            demo_pb2.Money,
            _drop_none({"currencyCode": cost.get("currency_code"), "units": cost.get("units"), "nanos": cost.get("nanos")}),
        )

    async def get_product_price(self, price: demo_pb2.Money | None, currency_code: str) -> demo_pb2.Money | None:
        if currency_code and currency_code != DEFAULT_CURRENCY_CODE:
            return await self.convert(price, currency_code)
        return price

    async def list_products(self, currency_code: str = DEFAULT_CURRENCY_CODE) -> list[demo_pb2.Product]:
        response = await self.product_catalog.ListProducts(demo_pb2.Empty())
        prices = await asyncio.gather(
            *(self.get_product_price(_price(p), currency_code) for p in response.products)
        )
        return [_with_price(p, price) for p, price in zip(response.products, prices)]

    async def get_product(self, product_id: str, currency_code: str = DEFAULT_CURRENCY_CODE) -> demo_pb2.Product:
        product = await self.product_catalog.GetProduct(demo_pb2.GetProductRequest(id=product_id))
        return _with_price(product, await self.get_product_price(_price(product), currency_code))


def _get(obj: Any, key: str) -> Any:
    return obj.get(key) if isinstance(obj, dict) else None


def _drop_none(data: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if v is not None}


def _price(product: demo_pb2.Product) -> demo_pb2.Money | None:
    return product.price_usd if product.HasField("price_usd") else None


def _with_price(product: demo_pb2.Product, price: demo_pb2.Money | None) -> demo_pb2.Product:
    result = demo_pb2.Product()
    result.CopyFrom(product)
    if price is None:
        result.ClearField("price_usd")
    else:
        result.price_usd.CopyFrom(price)
    return result
