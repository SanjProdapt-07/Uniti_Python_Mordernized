# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
import pytest
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

SPAN_EXPORTER = InMemorySpanExporter()
METRIC_READER = InMemoryMetricReader()

_tracer_provider = TracerProvider()
_tracer_provider.add_span_processor(SimpleSpanProcessor(SPAN_EXPORTER))
trace.set_tracer_provider(_tracer_provider)
metrics.set_meter_provider(MeterProvider(metric_readers=[METRIC_READER]))

from cart.telemetry import GrpcSpanNameProcessor  # noqa: E402

_tracer_provider.add_span_processor(GrpcSpanNameProcessor())


@pytest.fixture
def spans():
    SPAN_EXPORTER.clear()
    return SPAN_EXPORTER


@pytest.fixture
def metric_reader():
    return METRIC_READER


def metric_points(reader, name):
    data = reader.get_metrics_data()
    points = []
    if data is None:
        return points
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name == name:
                    points.extend(metric.data.data_points)
    return points
