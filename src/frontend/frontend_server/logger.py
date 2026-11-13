# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import json
import logging
import os
import socket
import sys
import time
import traceback

from opentelemetry import trace

PINO_LEVELS = {
    logging.DEBUG: 20,
    logging.INFO: 30,
    logging.WARNING: 40,
    logging.ERROR: 50,
    logging.CRITICAL: 60,
}

_RESERVED = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName"}
_OTEL_INJECTED = {"otelSpanID", "otelTraceID", "otelTraceSampled", "otelServiceName"}


class PinoFormatter(logging.Formatter):
    hostname = socket.gethostname()

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "level": PINO_LEVELS.get(record.levelno, 30),
            "time": int(record.created * 1000),
            "pid": os.getpid(),
            "hostname": self.hostname,
        }
        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            entry["trace_id"] = format(span_context.trace_id, "032x")
            entry["span_id"] = format(span_context.span_id, "016x")
            entry["trace_flags"] = format(span_context.trace_flags, "02x")
        if record.exc_info and record.exc_info[1] is not None:
            err = record.exc_info[1]
            entry["err"] = {
                "type": type(err).__name__,
                "message": str(err),
                "stack": "".join(traceback.format_exception(err)).rstrip(),
            }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key not in _OTEL_INJECTED:
                entry[key] = value
        entry["msg"] = record.getMessage()
        return json.dumps(entry, default=str)


def _level() -> int:
    name = os.environ.get("LOG_LEVEL", "info").upper()
    if name == "TRACE":
        return logging.DEBUG
    if name == "FATAL":
        return logging.CRITICAL
    if name == "WARN":
        return logging.WARNING
    return logging.getLevelNamesMapping().get(name, logging.INFO)


logger = logging.getLogger("frontend")
logger.setLevel(_level())
if not any(isinstance(h, logging.StreamHandler) and isinstance(h.formatter, PinoFormatter) for h in logger.handlers):
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(PinoFormatter())
    logger.addHandler(_handler)
