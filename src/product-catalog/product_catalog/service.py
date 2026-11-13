# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
import logging
import threading
from concurrent import futures

import grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc
from grpc_reflection.v1alpha import reflection
from openfeature.evaluation_context import EvaluationContext
from opentelemetry import trace
from opentelemetry.trace import StatusCode

import demo_pb2
import demo_pb2_grpc

from .telemetry import RpcServerMetricsInterceptor

logger = logging.getLogger("product-catalog")

PRODUCT_CATALOG_FAILURE = "productCatalogFailure"
PRODUCT_CATALOG_LOCK_CONTENTION = "productCatalogLockContention"


class ProductCatalogService(demo_pb2_grpc.ProductCatalogServiceServicer):
    def __init__(self, catalog, feature_client):
        self._catalog = catalog
        self._feature_client = feature_client

    def ListProducts(self, request, context):
        span = trace.get_current_span()
        try:
            products = self._catalog.list_products()
        except Exception as ex:
            span.set_status(StatusCode.ERROR, str(ex))
            context.abort(grpc.StatusCode.INTERNAL, f"failed to load products: {ex}")
        span.set_attribute("demo.product.count", len(products))
        return demo_pb2.ListProductsResponse(products=products)

    def GetProduct(self, request, context):
        span = trace.get_current_span()
        span.set_attribute("demo.product.id", request.id)

        if self._check_product_failure(request.id):
            msg = "Error: Product Catalog Fail Feature Flag Enabled"
            span.set_status(StatusCode.ERROR, msg)
            span.add_event(msg)
            context.abort(grpc.StatusCode.INTERNAL, msg)

        try:
            found = self._catalog.get_product(request.id)
        except Exception:
            msg = f"Product Not Found: {request.id}"
            span.set_status(StatusCode.ERROR, msg)
            span.add_event(msg)
            context.abort(grpc.StatusCode.NOT_FOUND, msg)

        span.add_event("Product Found")
        span.set_attribute("demo.product.id", request.id)
        span.set_attribute("demo.product.name", found.name)
        logger.info("Product Found", extra={"demo.product.name": found.name, "demo.product.id": request.id})
        return found

    def SearchProducts(self, request, context):
        span = trace.get_current_span()
        try:
            result = self._catalog.search_products(request.query)
        except Exception as ex:
            span.set_status(StatusCode.ERROR, str(ex))
            context.abort(grpc.StatusCode.INTERNAL, f"failed to search products: {ex}")
        span.set_attribute("demo.product.search.count", len(result))
        return demo_pb2.SearchProductsResponse(results=result)

    def _check_product_failure(self, product_id: str) -> bool:
        return self._feature_client.get_boolean_value(
            PRODUCT_CATALOG_FAILURE, False, EvaluationContext(attributes={"product_id": product_id}))


class LockContentionLoop:
    def __init__(self, catalog, feature_client, interval: float = 10, hold_seconds: float = 30):
        self._catalog = catalog
        self._feature_client = feature_client
        self._interval = interval
        self._hold_seconds = hold_seconds
        self._stop = threading.Event()
        self._locking = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="lock-contention", daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def tick(self):
        enabled = self._feature_client.get_boolean_value(PRODUCT_CATALOG_LOCK_CONTENTION, False, EvaluationContext())
        if enabled and self._locking.acquire(blocking=False):
            threading.Thread(target=self._hold, daemon=True).start()

    def _hold(self):
        try:
            self._catalog.hold_lock(self._stop, self._hold_seconds)
        finally:
            self._locking.release()

    def _run(self):
        while not self._stop.wait(self._interval):
            self.tick()


def build_server(catalog, feature_client, address: str, max_workers: int = 64, interceptors=None):
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=max_workers),
        interceptors=interceptors if interceptors is not None else [RpcServerMetricsInterceptor()],
    )
    demo_pb2_grpc.add_ProductCatalogServiceServicer_to_server(ProductCatalogService(catalog, feature_client), server)
    health_servicer = health.HealthServicer()
    health_servicer.set("", health_pb2.HealthCheckResponse.SERVING)
    health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)
    reflection.enable_server_reflection(
        (
            demo_pb2.DESCRIPTOR.services_by_name["ProductCatalogService"].full_name,
            health_pb2.DESCRIPTOR.services_by_name["Health"].full_name,
            reflection.SERVICE_NAME,
        ),
        server,
    )
    port = server.add_insecure_port(address)
    return server, port
