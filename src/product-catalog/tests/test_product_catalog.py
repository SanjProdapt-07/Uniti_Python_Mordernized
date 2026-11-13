# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
import contextlib
import threading

import grpc
import pytest
from grpc_health.v1 import health_pb2, health_pb2_grpc
from openfeature import api
from openfeature.flag_evaluation import FlagResolutionDetails
from openfeature.provider.in_memory_provider import InMemoryFlag, InMemoryProvider
from opentelemetry.instrumentation.grpc import server_interceptor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.trace import SpanKind, StatusCode

import demo_pb2
import demo_pb2_grpc
from conftest import metric_points
from product_catalog.catalog import (
    GET_PRODUCT_SQL,
    LIST_PRODUCTS_SQL,
    LOCK_SQL,
    SEARCH_PRODUCTS_SQL,
    CatalogError,
    PostgresCatalog,
    parse_product_row,
)
from product_catalog.metrics import register_pool_metrics
from product_catalog.service import LockContentionLoop, build_server
from product_catalog.telemetry import RpcServerMetricsInterceptor

ROWS = [
    ("66VCHSJNUP", "Starsense Explorer Refractor Telescope", "The first telescope.", "StarsenseExplorer.jpg",
     "USD", 349, 950000000, "telescopes"),
    ("OLJCESPC7Z", "National Park Foundation Explorascope", "The National Park Foundation's (NPF) Explorascope.",
     "NationalParkFoundationExplorascope.jpg", "USD", 101, 960000000, "telescopes, travel"),
    ("9SOIN3QJO0", "Solar System Color Imager", "You have your new telescope.", "SolarSystemColorImager.jpg",
     "USD", 175, 0, "accessories,telescopes"),
]


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self.result = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.db.executed.append((sql, params))
        if self.db.fail:
            raise RuntimeError("connection refused")
        if sql == LIST_PRODUCTS_SQL:
            self.result = sorted(self.db.rows)
        elif sql == SEARCH_PRODUCTS_SQL:
            needle = params["pattern"].strip("%")
            self.result = sorted(r for r in self.db.rows if needle in r[1].lower() or needle in (r[2] or "").lower())
        elif sql == GET_PRODUCT_SQL:
            self.result = [r for r in self.db.rows if r[0] == params["id"]]
        else:
            raise AssertionError(sql)

    def fetchall(self):
        return self.result

    def fetchone(self):
        return self.result[0] if self.result else None


class FakeConnection:
    def __init__(self, db):
        self.db = db

    def cursor(self):
        return FakeCursor(self.db)

    def execute(self, sql, params=None):
        self.db.executed.append((sql, params))

    @contextlib.contextmanager
    def transaction(self, force_rollback=False):
        self.db.transactions.append(force_rollback)
        yield


class FakePool:
    name = "product-catalog"
    max_size = 20

    def __init__(self, rows=ROWS, fail=False):
        self.rows = list(rows)
        self.fail = fail
        self.executed = []
        self.transactions = []

    @contextlib.contextmanager
    def connection(self):
        yield FakeConnection(self)

    def get_stats(self):
        return {"pool_size": 3, "pool_available": 1, "pool_max": 20, "requests_waiting": 0}


def flags(failure_for=None, lock=False):
    def failure(flag, ctx):
        on = failure_for is not None and ctx.attributes.get("product_id") == failure_for
        return FlagResolutionDetails(value=on, variant="on" if on else "off")

    return InMemoryProvider({
        "productCatalogFailure": InMemoryFlag("off", {"on": True, "off": False}, context_evaluator=failure),
        "productCatalogLockContention": InMemoryFlag("on" if lock else "off", {"on": True, "off": False}),
    })


@pytest.fixture
def catalog_server():
    started = []

    def start(pool=None, provider=None, meter_provider=None):
        api.set_provider(provider or flags())
        server, port = build_server(
            PostgresCatalog(pool if pool is not None else FakePool()), api.get_client(), "127.0.0.1:0",
            interceptors=[server_interceptor(), RpcServerMetricsInterceptor(meter_provider=meter_provider)],
        )
        server.start()
        channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        started.append((server, channel))
        return demo_pb2_grpc.ProductCatalogServiceStub(channel), channel

    yield start
    for server, channel in started:
        channel.close()
        server.stop(None)
    api.clear_providers()


def server_span(spans, name):
    found = [s for s in spans.get_finished_spans() if s.name == name and s.kind == SpanKind.SERVER]
    assert found, [s.name for s in spans.get_finished_spans()]
    return found[-1]


def test_parse_product_row_trims_categories():
    product = parse_product_row(ROWS[1])
    assert product.categories == ["telescopes", "travel"]
    assert product.price_usd == demo_pb2.Money(currency_code="USD", units=101, nanos=960000000)
    assert parse_product_row(ROWS[0][:7] + ("",)).categories == []
    with pytest.raises(CatalogError, match="failed to scan product row"):
        parse_product_row(ROWS[0][:3] + (None,) + ROWS[0][4:])


def test_list_products(catalog_server, spans):
    client, _ = catalog_server()
    response = client.ListProducts(demo_pb2.Empty())
    assert [p.id for p in response.products] == ["66VCHSJNUP", "9SOIN3QJO0", "OLJCESPC7Z"]
    span = server_span(spans, "oteldemo.ProductCatalogService/ListProducts")
    assert span.attributes["demo.product.count"] == 3


def test_list_products_database_error_is_internal(catalog_server, spans):
    client, _ = catalog_server(pool=FakePool(fail=True))
    with pytest.raises(grpc.RpcError) as error:
        client.ListProducts(demo_pb2.Empty())
    assert error.value.code() == grpc.StatusCode.INTERNAL
    assert error.value.details() == "failed to load products: failed to query products: connection refused"
    assert server_span(spans, "oteldemo.ProductCatalogService/ListProducts").status.status_code == StatusCode.ERROR


def test_search_products_lowercases_query_and_uses_like_pattern(catalog_server, spans):
    pool = FakePool()
    client, _ = catalog_server(pool=pool)
    response = client.SearchProducts(demo_pb2.SearchProductsRequest(query="TELESCOPE"))
    assert [p.id for p in response.results] == ["66VCHSJNUP", "9SOIN3QJO0"]
    assert pool.executed[-1] == (SEARCH_PRODUCTS_SQL, {"pattern": "%telescope%"})
    assert "LOWER(p.name) LIKE" in SEARCH_PRODUCTS_SQL and "LOWER(p.description) LIKE" in SEARCH_PRODUCTS_SQL
    assert server_span(spans, "oteldemo.ProductCatalogService/SearchProducts").attributes[
        "demo.product.search.count"] == 2


def test_search_products_database_error(catalog_server):
    client, _ = catalog_server(pool=FakePool(fail=True))
    with pytest.raises(grpc.RpcError) as error:
        client.SearchProducts(demo_pb2.SearchProductsRequest(query="x"))
    assert error.value.code() == grpc.StatusCode.INTERNAL
    assert error.value.details().startswith("failed to search products: ")


def test_get_product_found(catalog_server, spans):
    client, _ = catalog_server()
    product = client.GetProduct(demo_pb2.GetProductRequest(id="OLJCESPC7Z"))
    assert product.name == "National Park Foundation Explorascope"
    span = server_span(spans, "oteldemo.ProductCatalogService/GetProduct")
    assert span.attributes["demo.product.id"] == "OLJCESPC7Z"
    assert span.attributes["demo.product.name"] == product.name
    assert "Product Found" in [e.name for e in span.events]


def test_get_product_not_found(catalog_server, spans):
    client, _ = catalog_server()
    with pytest.raises(grpc.RpcError) as error:
        client.GetProduct(demo_pb2.GetProductRequest(id="NOPE"))
    assert error.value.code() == grpc.StatusCode.NOT_FOUND
    assert error.value.details() == "Product Not Found: NOPE"
    span = server_span(spans, "oteldemo.ProductCatalogService/GetProduct")
    assert span.status.status_code == StatusCode.ERROR
    assert "Product Not Found: NOPE" in [e.name for e in span.events]


def test_product_catalog_failure_flag_targets_product(catalog_server, spans):
    client, _ = catalog_server(provider=flags(failure_for="OLJCESPC7Z"))
    with pytest.raises(grpc.RpcError) as error:
        client.GetProduct(demo_pb2.GetProductRequest(id="OLJCESPC7Z"))
    assert error.value.code() == grpc.StatusCode.INTERNAL
    assert error.value.details() == "Error: Product Catalog Fail Feature Flag Enabled"
    span = server_span(spans, "oteldemo.ProductCatalogService/GetProduct")
    assert span.status.status_code == StatusCode.ERROR
    assert "Error: Product Catalog Fail Feature Flag Enabled" in [e.name for e in span.events]
    assert client.GetProduct(demo_pb2.GetProductRequest(id="66VCHSJNUP")).id == "66VCHSJNUP"


def test_health_and_rpc_server_duration_metric(catalog_server):
    reader = InMemoryMetricReader()
    client, channel = catalog_server(meter_provider=MeterProvider(metric_readers=[reader]))
    health = health_pb2_grpc.HealthStub(channel)
    assert health.Check(health_pb2.HealthCheckRequest()).status == health_pb2.HealthCheckResponse.SERVING
    client.ListProducts(demo_pb2.Empty())
    with pytest.raises(grpc.RpcError):
        client.GetProduct(demo_pb2.GetProductRequest(id="NOPE"))
    points = metric_points(reader, "rpc.server.duration")
    by_method = {p.attributes["rpc.method"]: p.attributes for p in points}
    assert set(by_method) == {"ListProducts", "GetProduct"}
    assert by_method["ListProducts"]["rpc.grpc.status_code"] == 0
    assert by_method["GetProduct"]["rpc.grpc.status_code"] == grpc.StatusCode.NOT_FOUND.value[0]
    assert by_method["GetProduct"]["rpc.service"] == "oteldemo.ProductCatalogService"


def test_reflection_lists_services(catalog_server):
    from grpc_reflection.v1alpha import reflection_pb2, reflection_pb2_grpc

    _, channel = catalog_server()
    stub = reflection_pb2_grpc.ServerReflectionStub(channel)
    responses = stub.ServerReflectionInfo(iter([reflection_pb2.ServerReflectionRequest(list_services="")]))
    names = {s.name for s in next(responses).list_services_response.service}
    assert "oteldemo.ProductCatalogService" in names and "grpc.health.v1.Health" in names


def test_lock_contention_holds_exclusive_lock_once_while_flag_on():
    pool = FakePool()
    api.set_provider(flags(lock=True))
    loop = LockContentionLoop(PostgresCatalog(pool), api.get_client(), hold_seconds=0.2)
    loop.tick()
    loop.tick()
    deadline = threading.Event()
    deadline.wait(0.5)
    assert [sql for sql, _ in pool.executed].count(LOCK_SQL) == 1
    assert pool.transactions == [True]
    loop.tick()
    deadline.wait(0.5)
    assert [sql for sql, _ in pool.executed].count(LOCK_SQL) == 2

    api.set_provider(flags(lock=False))
    loop.tick()
    deadline.wait(0.3)
    assert [sql for sql, _ in pool.executed].count(LOCK_SQL) == 2
    api.clear_providers()


def test_pool_metrics():
    reader = InMemoryMetricReader()
    register_pool_metrics(FakePool(), meter_provider=MeterProvider(metric_readers=[reader]))
    counts = {p.attributes["db.client.connection.state"]: p.value
              for p in metric_points(reader, "db.client.connection.count")}
    assert counts == {"idle": 1, "used": 2}
    assert metric_points(reader, "db.client.connection.max")[0].value == 20
