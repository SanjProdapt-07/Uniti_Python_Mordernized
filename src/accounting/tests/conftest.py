# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

_exporter = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(_exporter))
trace.set_tracer_provider(_provider)


@pytest.fixture
def spans():
    _exporter.clear()
    yield _exporter
    _exporter.clear()


@pytest.fixture
def producer_context():
    tracer = trace.get_tracer("checkout")
    with tracer.start_as_current_span("orders publish", kind=trace.SpanKind.PRODUCER) as span:
        from opentelemetry import propagate

        carrier = {}
        propagate.inject(carrier)
        headers = [(k, v.encode()) for k, v in carrier.items()]
        return span.get_span_context(), headers
