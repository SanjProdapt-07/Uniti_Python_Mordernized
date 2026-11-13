# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
import logging
import os
import time
import uuid

import grpc
from opentelemetry import metrics
from opentelemetry.sdk.trace import SpanProcessor

from .opamp import OPAMP_SERVER_ENDPOINT_ENV

OTEL_RESOURCE_ATTRIBUTES_ENV = "OTEL_RESOURCE_ATTRIBUTES"

ENV_DEFAULTS = {
    "OTEL_TRACES_EXPORTER": "otlp",
    "OTEL_METRICS_EXPORTER": "otlp",
    "OTEL_LOGS_EXPORTER": "otlp",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
    "OTEL_PROPAGATORS": "tracecontext,baggage",
    "OTEL_EXPERIMENTAL_RESOURCE_DETECTORS": "otel,host,process,containerid",
    "OTEL_METRICS_EXEMPLAR_FILTER": "trace_based",
    "OTEL_SEMCONV_STABILITY_OPT_IN": "database",
    "OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED": "true",
    "OTEL_PYTHON_GRPC_EXCLUDED_SERVICES": "grpc.health.v1.Health,flagd.evaluation.v1.Service,flagd.evaluation.v2.Service,flagd.sync.v1.FlagSyncService",
}


def parse_resource_attributes(value: str) -> dict:
    attributes = {}
    for pair in value.split(","):
        key, sep, val = pair.partition("=")
        if sep and key.strip():
            attributes[key.strip()] = val.strip()
    return attributes


def prepare_opamp_identity(environ=os.environ) -> bytes | None:
    if not environ.get(OPAMP_SERVER_ENDPOINT_ENV):
        return None
    instance_uid = os.urandom(16)
    current = environ.get(OTEL_RESOURCE_ATTRIBUTES_ENV, "")
    if parse_resource_attributes(current).get("service.instance.id"):
        return instance_uid
    if current:
        current += ","
    environ[OTEL_RESOURCE_ATTRIBUTES_ENV] = current + "service.instance.id=" + instance_uid.hex()
    return instance_uid


def prepare_environment(environ=os.environ) -> None:
    for key, value in ENV_DEFAULTS.items():
        environ.setdefault(key, value)
    current = environ.get(OTEL_RESOURCE_ATTRIBUTES_ENV, "")
    if not parse_resource_attributes(current).get("service.instance.id"):
        instance_id = str(uuid.uuid4())
        environ[OTEL_RESOURCE_ATTRIBUTES_ENV] = (
            f"{current},service.instance.id={instance_id}" if current else f"service.instance.id={instance_id}"
        )


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


class RpcServerMetricsInterceptor(grpc.ServerInterceptor):
    def __init__(self, meter_provider=None, excluded_services=("grpc.health.v1.Health",)):
        meter = metrics.get_meter(
            "go.opentelemetry.io/contrib/instrumentation/google.golang.org/grpc/otelgrpc", meter_provider=meter_provider)
        self._duration = meter.create_histogram(
            "rpc.server.duration", unit="ms", description="Measures the duration of inbound RPC.")
        self._excluded = tuple(excluded_services)

    def intercept_service(self, continuation, handler_call_details):
        handler = continuation(handler_call_details)
        service, _, method = handler_call_details.method.lstrip("/").partition("/")
        if handler is None or not handler.unary_unary or service in self._excluded:
            return handler
        behavior = handler.unary_unary

        def wrapper(request, context):
            start = time.perf_counter()
            failed = False
            try:
                return behavior(request, context)
            except Exception:
                failed = True
                raise
            finally:
                code = _status_code(context, failed)
                self._duration.record((time.perf_counter() - start) * 1000, {
                    "rpc.system": "grpc",
                    "rpc.service": service,
                    "rpc.method": method,
                    "rpc.grpc.status_code": code.value[0],
                })

        return grpc.unary_unary_rpc_method_handler(wrapper, handler.request_deserializer, handler.response_serializer)


def _status_code(context, failed: bool) -> grpc.StatusCode:
    code = None
    try:
        code = context.code()
    except Exception:
        pass
    if isinstance(code, grpc.StatusCode):
        return code
    return grpc.StatusCode.UNKNOWN if failed else grpc.StatusCode.OK
