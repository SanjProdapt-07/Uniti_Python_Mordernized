# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import json
from concurrent import futures
from pathlib import Path

import grpc
import httpx
import pytest
from fastapi.testclient import TestClient

import demo_pb2
import demo_pb2_grpc
from frontend_server.app import create_app
from frontend_server.pages import ENV_PLACEHOLDER

PRODUCTS = {
    "OLJCESPC7Z": demo_pb2.Product(
        id="OLJCESPC7Z",
        name="National Park Foundation Explorascope",
        description="The Explorascope",
        picture="NationalParkFoundationExplorascope.jpg",
        price_usd=demo_pb2.Money(currency_code="USD", units=101, nanos=960000000),
        categories=["telescopes"],
    ),
    "66VCHSJNUP": demo_pb2.Product(
        id="66VCHSJNUP",
        name="Starsense Explorer",
        description="Starsense",
        picture="StarsenseExplorer.jpg",
        price_usd=demo_pb2.Money(currency_code="USD", units=349, nanos=950000000),
        categories=["telescopes"],
    ),
    "NOPRICE": demo_pb2.Product(id="NOPRICE", name="No price"),
}


class Fakes:
    def __init__(self):
        self.requests: list[tuple[str, object]] = []
        self.carts: dict[str, list[demo_pb2.CartItem]] = {}
        self.recommended = ["66VCHSJNUP", "OLJCESPC7Z", "66VCHSJNUP", "OLJCESPC7Z", "66VCHSJNUP"]
        self.checkout_error: tuple[grpc.StatusCode, str] | None = None
        self.order_items: list[demo_pb2.OrderItem] | None = None
        self.fail_product: str | None = None
        self.metadata: list[tuple[str, str]] = []

    def record(self, name, request, context):
        self.requests.append((name, request))
        self.metadata = list(context.invocation_metadata())

    def calls(self, name):
        return [r for n, r in self.requests if n == name]


class CartServicer(demo_pb2_grpc.CartServiceServicer):
    def __init__(self, fakes):
        self.fakes = fakes

    def AddItem(self, request, context):
        self.fakes.record("AddItem", request, context)
        self.fakes.carts.setdefault(request.user_id, []).append(request.item)
        return demo_pb2.Empty()

    def GetCart(self, request, context):
        self.fakes.record("GetCart", request, context)
        return demo_pb2.Cart(user_id=request.user_id, items=self.fakes.carts.get(request.user_id, []))

    def EmptyCart(self, request, context):
        self.fakes.record("EmptyCart", request, context)
        self.fakes.carts.pop(request.user_id, None)
        return demo_pb2.Empty()


class ProductCatalogServicer(demo_pb2_grpc.ProductCatalogServiceServicer):
    def __init__(self, fakes):
        self.fakes = fakes

    def ListProducts(self, request, context):
        self.fakes.record("ListProducts", request, context)
        return demo_pb2.ListProductsResponse(products=[PRODUCTS["OLJCESPC7Z"], PRODUCTS["66VCHSJNUP"]])

    def GetProduct(self, request, context):
        self.fakes.record("GetProduct", request, context)
        if request.id == self.fakes.fail_product or request.id not in PRODUCTS:
            context.abort(grpc.StatusCode.NOT_FOUND, f"Product Not Found: {request.id}")
        return PRODUCTS[request.id]


class CurrencyServicer(demo_pb2_grpc.CurrencyServiceServicer):
    def __init__(self, fakes):
        self.fakes = fakes

    def GetSupportedCurrencies(self, request, context):
        self.fakes.record("GetSupportedCurrencies", request, context)
        return demo_pb2.GetSupportedCurrenciesResponse(currency_codes=["USD", "EUR", "CHF"])

    def Convert(self, request, context):
        self.fakes.record("Convert", request, context)
        source = getattr(request, "from")
        return demo_pb2.Money(currency_code=request.to_code, units=source.units * 2, nanos=source.nanos // 2)


class RecommendationServicer(demo_pb2_grpc.RecommendationServiceServicer):
    def __init__(self, fakes):
        self.fakes = fakes

    def ListRecommendations(self, request, context):
        self.fakes.record("ListRecommendations", request, context)
        return demo_pb2.ListRecommendationsResponse(product_ids=self.fakes.recommended)


class AdServicer(demo_pb2_grpc.AdServiceServicer):
    def __init__(self, fakes):
        self.fakes = fakes

    def GetAds(self, request, context):
        self.fakes.record("GetAds", request, context)
        return demo_pb2.AdResponse(
            ads=[demo_pb2.Ad(redirect_url=f"/product/{k}", text=f"Ad for {k}") for k in request.context_keys]
        )


class CheckoutServicer(demo_pb2_grpc.CheckoutServiceServicer):
    def __init__(self, fakes):
        self.fakes = fakes

    def PlaceOrder(self, request, context):
        self.fakes.record("PlaceOrder", request, context)
        if self.fakes.checkout_error:
            context.abort(*self.fakes.checkout_error)
        items = self.fakes.order_items
        if items is None:
            items = [
                demo_pb2.OrderItem(item=i, cost=PRODUCTS[i.product_id].price_usd)
                for i in self.fakes.carts.get(request.user_id, [])
            ]
        return demo_pb2.PlaceOrderResponse(
            order=demo_pb2.OrderResult(
                order_id="order-1",
                shipping_tracking_id="track-1",
                shipping_cost=demo_pb2.Money(currency_code="USD", units=8, nanos=990000000),
                shipping_address=request.address,
                items=items,
            )
        )


@pytest.fixture(scope="session")
def grpc_backend():
    fakes = Fakes()
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
    demo_pb2_grpc.add_CartServiceServicer_to_server(CartServicer(fakes), server)
    demo_pb2_grpc.add_ProductCatalogServiceServicer_to_server(ProductCatalogServicer(fakes), server)
    demo_pb2_grpc.add_CurrencyServiceServicer_to_server(CurrencyServicer(fakes), server)
    demo_pb2_grpc.add_RecommendationServiceServicer_to_server(RecommendationServicer(fakes), server)
    demo_pb2_grpc.add_AdServiceServicer_to_server(AdServicer(fakes), server)
    demo_pb2_grpc.add_CheckoutServiceServicer_to_server(CheckoutServicer(fakes), server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    yield fakes, f"127.0.0.1:{port}"
    server.stop(None)


@pytest.fixture
def fakes(grpc_backend):
    fakes, _ = grpc_backend
    fakes.__init__()
    return fakes


class Shipping:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.body: dict = {"cost_usd": {"currency_code": "USD", "units": 8, "nanos": 990000000}}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, text="quote failed")
        return httpx.Response(200, json=self.body)


@pytest.fixture
def shipping():
    return Shipping()


PAGE_NAMES = {
    "index.html": "Home",
    "cart.html": "Cart",
    "product/[productId].html": "Product",
    "cart/checkout/[orderId].html": "Checkout",
    "404.html": "Page Not Found",
    "500.html": "Server Error",
}


@pytest.fixture
def static_dir(tmp_path: Path) -> Path:
    out = tmp_path / "out"
    for name, title in PAGE_NAMES.items():
        page = out / name
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(
            f"<!DOCTYPE html><html><head><title>Otel Demo - {title}</title></head>"
            f'<body><div id="__next"></div><script>{ENV_PLACEHOLDER}</script>'
            f'<script src="/_next/static/chunks/main.js" defer=""></script></body></html>'
        )
    (out / "_next/static/chunks").mkdir(parents=True)
    (out / "_next/static/chunks/main.js").write_text("console.log('main');")
    (out / "icons").mkdir()
    (out / "icons/Cart.svg").write_text("<svg></svg>")
    (out / "favicon.ico").write_bytes(b"\x00\x00\x01\x00")
    return out


ENV = {
    "ENV_PLATFORM": "local",
    "WEB_OTEL_SERVICE_NAME": "frontend-web",
    "PUBLIC_OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://localhost:8080/otlp-http/v1/traces",
    "OTEL_COLLECTOR_HOST": "otel-collector",
    "SHIPPING_ADDR": "http://shipping:50050",
}


@pytest.fixture
def client(grpc_backend, fakes, shipping, static_dir):
    _, address = grpc_backend
    env = dict(ENV)
    for name in ("AD_ADDR", "CART_ADDR", "CHECKOUT_ADDR", "CURRENCY_ADDR", "PRODUCT_CATALOG_ADDR", "RECOMMENDATION_ADDR"):
        env[name] = address
    app = create_app(env, static_dir, httpx.MockTransport(shipping.handler))
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


def body_of(response) -> object:
    return json.loads(response.content)
