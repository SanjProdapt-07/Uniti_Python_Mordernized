# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
import logging
import os
import time
import uuid

import grpc
from opentelemetry import metrics
from opentelemetry.sdk.trace import SpanProcessor

ENV_DEFAULTS = {
    "OTEL_TRACES_EXPORTER": "otlp",
    "OTEL_METRICS_EXPORTER": "otlp",
    "OTEL_LOGS_EXPORTER": "otlp",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
    "OTEL_EXPERIMENTAL_RESOURCE_DETECTORS": "otel,host,containerid",
    "OTEL_METRICS_EXEMPLAR_FILTER": "trace_based",
}

HTTP_DURATION_BUCKETS = [0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1, 2.5, 5, 7.5, 10]


def parse_resource_attributes(value: str) -> dict:
    attributes = {}
    for pair in value.split(","):
        key, sep, val = pair.partition("=")
        if sep and key.strip():
            attributes[key.strip()] = val.strip()
    return attributes


def prepare_environment(environ=os.environ) -> str:
    for key, value in ENV_DEFAULTS.items():
        environ.setdefault(key, value)
    service_instance_id = str(uuid.uuid4())
    current = environ.get("OTEL_RESOURCE_ATTRIBUTES", "")
    environ["OTEL_RESOURCE_ATTRIBUTES"] = (
        f"{current},service.instance.id={service_instance_id}" if current else f"service.instance.id={service_instance_id}"
    )
    return service_instance_id


def configure_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(console)


class GrpcSpanNameProcessor(SpanProcessor):
    def on_start(self, span, parent_context=None):
        if span.name.startswith("/") and span.attributes.get("rpc.system") == "grpc":
            span.update_name(span.name[1:])


class HttpServerMetricsInterceptor(grpc.ServerInterceptor):
    def __init__(self, meter_provider=None):
        meter = metrics.get_meter("Microsoft.AspNetCore.Hosting", meter_provider=meter_provider)
        self._duration = meter.create_histogram(
            "http.server.request.duration",
            unit="s",
            description="Duration of HTTP server requests.",
            explicit_bucket_boundaries_advisory=HTTP_DURATION_BUCKETS,
        )

    def intercept_service(self, continuation, handler_call_details):
        handler = continuation(handler_call_details)
        if handler is None:
            return handler
        attributes = {
            "http.request.method": "POST",
            "http.route": handler_call_details.method,
            "http.response.status_code": 200,
            "network.protocol.version": "2",
            "url.scheme": "http",
        }

        def timed(behavior):
            def wrapper(request, context):
                start = time.perf_counter()
                try:
                    return behavior(request, context)
                finally:
                    self._duration.record(time.perf_counter() - start, attributes)
            return wrapper

        def timed_stream(behavior):
            def wrapper(request, context):
                start = time.perf_counter()
                try:
                    yield from behavior(request, context)
                finally:
                    self._duration.record(time.perf_counter() - start, attributes)
            return wrapper

        if handler.unary_unary:
            return grpc.unary_unary_rpc_method_handler(
                timed(handler.unary_unary), handler.request_deserializer, handler.response_serializer)
        if handler.unary_stream:
            return grpc.unary_stream_rpc_method_handler(
                timed_stream(handler.unary_stream), handler.request_deserializer, handler.response_serializer)
        return handler
