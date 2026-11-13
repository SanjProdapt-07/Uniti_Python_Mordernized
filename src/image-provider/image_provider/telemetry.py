# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_ON

SPAN_NAME = "image-provider"
SCOPE_NAME = "image_provider"


def collector_endpoint() -> str | None:
    if os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return None
    host = os.environ.get("OTEL_COLLECTOR_HOST")
    if not host:
        return None
    return f"http://{host}:{os.environ.get('OTEL_COLLECTOR_PORT_GRPC', '4317')}"


def configure_tracing() -> TracerProvider | None:
    if os.environ.get("OTEL_SDK_DISABLED", "").strip().lower() == "true":
        return None
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

    sampler = None if os.environ.get("OTEL_TRACES_SAMPLER") else ALWAYS_ON
    provider = TracerProvider(resource=Resource.create(), sampler=sampler)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=collector_endpoint())))
    trace.set_tracer_provider(provider)
    return provider
