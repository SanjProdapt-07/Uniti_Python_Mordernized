# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import pytest
from opentelemetry import _logs, metrics, propagate, trace
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

SPANS = InMemorySpanExporter()
METRICS = InMemoryMetricReader()
LOGS = InMemoryLogRecordExporter()

_tp = TracerProvider()
_tp.add_span_processor(SimpleSpanProcessor(SPANS))
trace.set_tracer_provider(_tp)
metrics.set_meter_provider(MeterProvider(metric_readers=[METRICS]))
_lp = LoggerProvider()
_lp.add_log_record_processor(SimpleLogRecordProcessor(LOGS))
_logs.set_logger_provider(_lp)

import logging  # noqa: E402

from opentelemetry.sdk._logs import LoggingHandler  # noqa: E402

logging.getLogger().addHandler(LoggingHandler(logger_provider=_lp))


class FakeContext:
    def __init__(self, metadata=()):
        self._metadata = tuple(metadata)
        self.code = None
        self.details = None

    def invocation_metadata(self):
        return self._metadata

    def abort(self, code, details):
        self.code = code
        self.details = details
        raise RuntimeError(f"aborted {code} {details}")


@pytest.fixture
def telemetry():
    SPANS.clear()
    LOGS.clear()
    yield SPANS, METRICS, LOGS


@pytest.fixture
def fake_context():
    return FakeContext


def traceparent_metadata(span_context):
    carrier = {}
    with trace.use_span(trace.NonRecordingSpan(span_context)):
        propagate.inject(carrier)
    return tuple(carrier.items())
