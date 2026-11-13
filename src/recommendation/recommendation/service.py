# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
import logging
import random
import threading
from concurrent import futures

import grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc
from grpc_reflection.v1alpha import reflection
from opentelemetry import metrics, trace
from opentelemetry.trace import StatusCode

import demo_pb2
import demo_pb2_grpc

from .telemetry import RpcServerMetricsInterceptor

logger = logging.getLogger("oteldemo.recommendation.RecommendationService")

CACHE_FAILURE_FLAG = "recommendationCacheFailure"
MAX_RESPONSES = 5
CATALOG_RECOMMENDATION = {"recommendation.type": "catalog"}

_tracer = trace.get_tracer("recommendation")
_recommendations = metrics.get_meter("recommendation").create_counter(
    "demo.recommendation.requests",
    unit="{recommendation}",
    description="Counts the total number of given recommendations",
)


def requested_ids(request_product_ids) -> set:
    ids = list(request_product_ids)
    one_character_each = bool(ids) and all(len(i) == 1 for i in ids)
    joined = ("" if one_character_each else ",").join(ids)
    return set(joined.split(","))


def java_list(values) -> str:
    return "[" + ", ".join(values) + "]"


class RecommendationService(demo_pb2_grpc.RecommendationServiceServicer):
    def __init__(self, product_catalog, feature_client, catalog_timeout_ms: int = 2000, rng: random.Random | None = None):
        if catalog_timeout_ms <= 0:
            raise ValueError("recommendation.catalog-timeout-ms must be positive")
        self._product_catalog = product_catalog
        self._feature_client = feature_client
        self._catalog_timeout = catalog_timeout_ms / 1000
        self._rng = rng or random.SystemRandom()
        self._cached_ids: list[str] = []
        self._first_run = True
        self._cache_lock = threading.Lock()

    def ListRecommendations(self, request, context):
        try:
            product_ids = self._get_product_list(request.product_ids)
        except grpc.RpcError as ex:
            context.abort(ex.code(), ex.details() or "")
        trace.get_current_span().set_attribute("demo.product.recommended.count", len(product_ids))
        logger.info(f"Receive ListRecommendations for product ids:{java_list(product_ids)}")
        _recommendations.add(len(product_ids), CATALOG_RECOMMENDATION)
        return demo_pb2.ListRecommendationsResponse(product_ids=product_ids)

    def _get_product_list(self, request_product_ids) -> list[str]:
        with _tracer.start_as_current_span("get_product_list", record_exception=False,
                                           set_status_on_exception=False) as span:
            try:
                if self._feature_client.get_boolean_value(CACHE_FAILURE_FLAG, False):
                    span.set_attribute("demo.feature_flag.recommendation_cache", True)
                    count, unique = self._get_products_from_leaky_cache(span)
                else:
                    span.set_attribute("demo.feature_flag.recommendation_cache", False)
                    fresh = self._fetch_product_ids()
                    count, unique = len(fresh), set(fresh)
                span.set_attribute("demo.product.count", count)

                filtered = [i for i in unique if i not in requested_ids(request_product_ids)]
                span.set_attribute("demo.product.filtered.count", len(filtered))

                self._rng.shuffle(filtered)
                recommended = filtered[:MAX_RESPONSES]
                span.set_attribute("demo.product.filtered.list", recommended)
                return recommended
            except Exception as ex:
                span.record_exception(ex)
                span.set_status(StatusCode.ERROR)
                raise

    def _get_products_from_leaky_cache(self, span):
        with self._cache_lock:
            miss = self._first_run or self._rng.random() < 0.5
            self._first_run = False
        if miss:
            span.set_attribute("demo.recommendation.cache_hit", False)
            logger.info("get_product_list: cache miss")
            fresh = self._fetch_product_ids()
            with self._cache_lock:
                self._cached_ids.extend(fresh)
                self._cached_ids.extend(self._cached_ids[: len(self._cached_ids) // 4])
                return len(self._cached_ids), set(self._cached_ids)
        span.set_attribute("demo.recommendation.cache_hit", True)
        logger.info("get_product_list: cache hit")
        with self._cache_lock:
            return len(self._cached_ids), set(self._cached_ids)

    def _fetch_product_ids(self) -> list[str]:
        response = self._product_catalog.ListProducts(demo_pb2.Empty(), timeout=self._catalog_timeout)
        return [p.id for p in response.products]


def build_server(product_catalog, feature_client, address: str, catalog_timeout_ms: int = 2000,
                 max_workers: int = 32, interceptors=None):
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=max_workers),
        interceptors=interceptors if interceptors is not None else [RpcServerMetricsInterceptor()],
    )
    demo_pb2_grpc.add_RecommendationServiceServicer_to_server(
        RecommendationService(product_catalog, feature_client, catalog_timeout_ms), server)
    health_servicer = health.HealthServicer()
    health_servicer.set("", health_pb2.HealthCheckResponse.SERVING)
    health_servicer.set("oteldemo.RecommendationService", health_pb2.HealthCheckResponse.SERVING)
    health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)
    reflection.enable_server_reflection(
        (
            demo_pb2.DESCRIPTOR.services_by_name["RecommendationService"].full_name,
            health_pb2.DESCRIPTOR.services_by_name["Health"].full_name,
            reflection.SERVICE_NAME,
        ),
        server,
    )
    port = server.add_insecure_port(address)
    return server, port
