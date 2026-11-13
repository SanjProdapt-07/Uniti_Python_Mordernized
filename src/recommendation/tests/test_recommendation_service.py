# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
import threading
import time
from concurrent import futures

import grpc
import pytest
from grpc_health.v1 import health_pb2, health_pb2_grpc
from openfeature import api
from openfeature.contrib.hook.opentelemetry import TracingHook
from openfeature.provider.in_memory_provider import InMemoryFlag, InMemoryProvider
from opentelemetry.instrumentation.grpc import client_interceptor, grpcext, server_interceptor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.trace import SpanKind, StatusCode

import demo_pb2
import demo_pb2_grpc
from conftest import METRIC_READER, metric_points
from recommendation.service import RecommendationService, build_server, requested_ids
from recommendation.telemetry import RpcClientMetricsInterceptor, RpcServerMetricsInterceptor

CATALOG = ["0PUK6V6EV0", "1YMWWN1N4O", "2ZYFJ3GM2N", "66VCHSJNUP", "6E92ZMJLZN", "9SOIN3QJO0", "L9ECAV7KIM"]


class FakeProductCatalog(demo_pb2_grpc.ProductCatalogServiceServicer):
    def __init__(self):
        self.down = False
        self.stalled = threading.Event()
        self.release = threading.Event()

    def ListProducts(self, request, context):
        if self.stalled.is_set():
            self.release.wait(10)
            return demo_pb2.ListProductsResponse()
        if self.down:
            context.abort(grpc.StatusCode.UNAVAILABLE, "catalog down")
        return demo_pb2.ListProductsResponse(products=[demo_pb2.Product(id=i, name=i) for i in CATALOG])


def set_cache_failure_flag(enabled: bool):
    api.set_provider(InMemoryProvider({
        "recommendationCacheFailure": InMemoryFlag("on" if enabled else "off", {"on": True, "off": False}),
    }))


@pytest.fixture
def env(spans):
    catalog = FakeProductCatalog()
    catalog_server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    demo_pb2_grpc.add_ProductCatalogServiceServicer_to_server(catalog, catalog_server)
    catalog_port = catalog_server.add_insecure_port("127.0.0.1:0")
    catalog_server.start()

    set_cache_failure_flag(False)
    api.add_hooks([TracingHook()])
    client_metrics = InMemoryMetricReader()
    traced_channel = grpcext.intercept_channel(grpc.insecure_channel(f"127.0.0.1:{catalog_port}"), client_interceptor())
    catalog_channel = grpc.intercept_channel(
        traced_channel,
        RpcClientMetricsInterceptor(meter_provider=MeterProvider(metric_readers=[client_metrics])),
    )
    server_metrics = InMemoryMetricReader()
    server, port = build_server(
        demo_pb2_grpc.ProductCatalogServiceStub(catalog_channel), api.get_client(), "127.0.0.1:0",
        interceptors=[server_interceptor(),
                      RpcServerMetricsInterceptor(meter_provider=MeterProvider(metric_readers=[server_metrics]))],
    )
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    yield {
        "catalog": catalog,
        "stub": demo_pb2_grpc.RecommendationServiceStub(channel),
        "channel": channel,
        "server_metrics": server_metrics,
        "client_metrics": client_metrics,
    }
    catalog.release.set()
    channel.close()
    server.stop(None)
    catalog_channel.close()
    catalog_server.stop(None)
    api.clear_hooks()
    api.clear_providers()


def list_recommendations(env, product_ids):
    return env["stub"].ListRecommendations(
        demo_pb2.ListRecommendationsRequest(user_id="test-user", product_ids=product_ids), timeout=5)


def span_named(spans, name, kind):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        for span in spans.get_finished_spans():
            if span.name == name and span.kind == kind:
                return span
        time.sleep(0.02)
    raise AssertionError(f"{name} not found in {[s.name for s in spans.get_finished_spans()]}")


def recommendations_counted():
    return sum(p.value for p in metric_points(METRIC_READER, "demo.recommendation.requests")
               if p.attributes.get("recommendation.type") == "catalog")


def test_recommends_up_to_five_other_products(env):
    ids = list(list_recommendations(env, ["66VCHSJNUP"]).product_ids)
    assert len(ids) == 5
    assert len(set(ids)) == 5
    assert "66VCHSJNUP" not in ids
    assert set(ids) <= set(CATALOG)


def test_leaves_out_every_requested_product_sent_the_way_the_frontend_sends_them(env):
    one_element_per_character = list("66VCHSJNUP,0PUK6V6EV0,1YMWWN1N4O,2ZYFJ3GM2N")
    ids = list_recommendations(env, one_element_per_character).product_ids
    assert sorted(ids) == ["6E92ZMJLZN", "9SOIN3QJO0", "L9ECAV7KIM"]


def test_leaves_out_every_requested_product_sent_as_whole_ids(env):
    ids = list_recommendations(env, ["66VCHSJNUP", "0PUK6V6EV0,1YMWWN1N4O", "2ZYFJ3GM2N"]).product_ids
    assert sorted(ids) == ["6E92ZMJLZN", "9SOIN3QJO0", "L9ECAV7KIM"]


def test_records_the_error_when_the_catalog_is_down(env, spans):
    env["catalog"].down = True
    with pytest.raises(grpc.RpcError) as error:
        list_recommendations(env, ["66VCHSJNUP"])
    assert error.value.code() == grpc.StatusCode.UNAVAILABLE
    assert "catalog down" in error.value.details()
    product_list = span_named(spans, "get_product_list", SpanKind.INTERNAL)
    assert product_list.status.status_code == StatusCode.ERROR
    assert any(e.name == "exception" for e in product_list.events)


def test_bounds_the_wait_for_a_catalog_that_never_replies(env, spans):
    env["catalog"].stalled.set()
    start = time.monotonic()
    with pytest.raises(grpc.RpcError) as error:
        list_recommendations(env, ["66VCHSJNUP"])
    assert time.monotonic() - start < 4
    assert error.value.code() == grpc.StatusCode.DEADLINE_EXCEEDED
    product_list = span_named(spans, "get_product_list", SpanKind.INTERNAL)
    assert product_list.status.status_code == StatusCode.ERROR
    assert any(e.name == "exception" for e in product_list.events)


def test_records_span_attributes_and_counter(env, spans):
    before = recommendations_counted()
    list_recommendations(env, ["66VCHSJNUP"])

    server_span = span_named(spans, "oteldemo.RecommendationService/ListRecommendations", SpanKind.SERVER)
    assert server_span.attributes["demo.product.recommended.count"] == 5

    product_list = span_named(spans, "get_product_list", SpanKind.INTERNAL)
    assert product_list.parent.span_id == server_span.context.span_id
    assert product_list.attributes["demo.feature_flag.recommendation_cache"] is False
    assert product_list.attributes["demo.product.count"] == 7
    assert product_list.attributes["demo.product.filtered.count"] == 6
    filtered = product_list.attributes["demo.product.filtered.list"]
    assert len(filtered) == 5 and set(filtered) <= set(CATALOG)
    assert "feature_flag.evaluation" in [e.name for e in product_list.events]

    catalog_call = span_named(spans, "oteldemo.ProductCatalogService/ListProducts", SpanKind.CLIENT)
    assert catalog_call.context.trace_id == server_span.context.trace_id

    assert recommendations_counted() == before + 5

    server_points = metric_points(env["server_metrics"], "rpc.server.duration")
    assert server_points[0].attributes["rpc.method"] == "ListRecommendations"
    client_points = metric_points(env["client_metrics"], "rpc.client.duration")
    assert client_points[0].attributes["rpc.service"] == "oteldemo.ProductCatalogService"


def test_leaky_cache_grows_while_the_feature_flag_is_on(env, spans):
    set_cache_failure_flag(True)
    list_recommendations(env, ["66VCHSJNUP"])
    first = span_named(spans, "get_product_list", SpanKind.INTERNAL)
    assert first.attributes["demo.feature_flag.recommendation_cache"] is True
    assert first.attributes["demo.recommendation.cache_hit"] is False
    assert first.attributes["demo.product.count"] == 8

    spans.clear()
    list_recommendations(env, ["66VCHSJNUP"])
    second = span_named(spans, "get_product_list", SpanKind.INTERNAL)
    assert "demo.recommendation.cache_hit" in second.attributes
    assert second.attributes["demo.product.count"] >= 8


def test_leaky_cache_growth_sequence():
    class Catalog:
        def ListProducts(self, request, timeout):
            return demo_pb2.ListProductsResponse(products=[demo_pb2.Product(id=i) for i in CATALOG])

    class AlwaysMiss:
        def random(self):
            return 0.0

        def shuffle(self, values):
            pass

    set_cache_failure_flag(True)
    service = RecommendationService(Catalog(), api.get_client(), rng=AlwaysMiss())
    service._get_product_list(["X"])
    assert len(service._cached_ids) == 8
    service._get_product_list(["X"])
    assert len(service._cached_ids) == 18
    api.clear_providers()


def test_requested_ids():
    assert requested_ids(list("A1,B2")) == {"A1", "B2"}
    assert requested_ids(["A1", "B2,C3"]) == {"A1", "B2", "C3"}
    assert requested_ids([]) == {""}


def test_timeout_must_be_positive():
    with pytest.raises(ValueError, match="must be positive"):
        RecommendationService(None, None, catalog_timeout_ms=0)


def test_health_serving(env):
    health = health_pb2_grpc.HealthStub(env["channel"])
    assert health.Check(health_pb2.HealthCheckRequest()).status == health_pb2.HealthCheckResponse.SERVING
