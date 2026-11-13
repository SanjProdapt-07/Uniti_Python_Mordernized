# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import logging
import sys
import time

from opentelemetry import trace

LOGGER_NAME = "fraud-detection"

logger = logging.getLogger(LOGGER_NAME)


class PatternFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            trace_id = format(span_context.trace_id, "032x")
            span_id = format(span_context.span_id, "016x")
            trace_flags = "01" if span_context.trace_flags.sampled else "00"
        else:
            trace_id = span_id = trace_flags = ""
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created))
        line = (
            f"{timestamp} - {record.name[-36:]} - {record.getMessage()} "
            f"trace_id={trace_id} span_id={span_id} trace_flags={trace_flags} "
        )
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def configure_console(stream=None) -> None:
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(PatternFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
