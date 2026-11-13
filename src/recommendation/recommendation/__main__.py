# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
import logging
import os
import signal
import threading

from .telemetry import prepare_environment


def main() -> None:
    port = os.environ.get("RECOMMENDATION_PORT") or "9001"
    catalog_addr = os.environ.get("PRODUCT_CATALOG_ADDR") or "localhost:3550"
    catalog_timeout_ms = int(os.environ.get("RECOMMENDATION_CATALOG_TIMEOUT_MS") or "2000")
    if catalog_timeout_ms <= 0:
        raise ValueError("recommendation.catalog-timeout-ms must be positive")

    prepare_environment()

    from opentelemetry.instrumentation import auto_instrumentation

    auto_instrumentation.initialize()

    import grpc
    from openfeature import api
    from openfeature.contrib.hook.opentelemetry import TracingHook
    from openfeature.contrib.provider.flagd import FlagdProvider
    from opentelemetry import trace

    import demo_pb2_grpc

    from .service import build_server
    from .telemetry import GrpcSpanNameProcessor, RpcClientMetricsInterceptor, configure_logging

    configure_logging()
    logger = logging.getLogger("oteldemo.recommendation.RecommendationApplication")

    tracer_provider = trace.get_tracer_provider()
    if hasattr(tracer_provider, "add_span_processor"):
        tracer_provider.add_span_processor(GrpcSpanNameProcessor())

    api.set_provider(FlagdProvider())
    api.add_hooks([TracingHook()])
    feature_client = api.get_client()

    channel = grpc.intercept_channel(grpc.insecure_channel(catalog_addr), RpcClientMetricsInterceptor())
    product_catalog = demo_pb2_grpc.ProductCatalogServiceStub(channel)

    server, _ = build_server(product_catalog, feature_client, f"[::]:{port}", catalog_timeout_ms)
    server.start()
    logger.info(f"gRPC Server started, listening on address: *, port: {port}")

    stopped = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopped.set())
    stopped.wait()
    server.stop(grace=30).wait()
    channel.close()
    api.shutdown()


if __name__ == "__main__":
    main()
