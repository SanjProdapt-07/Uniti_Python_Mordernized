# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import json
import logging
import os
import socket
import sys
import traceback

from opentelemetry import trace

_LEVELS = {
    logging.DEBUG: "debug",
    logging.INFO: "info",
    logging.WARNING: "warn",
    logging.ERROR: "error",
    logging.CRITICAL: "fatal",
}

_STANDARD_ATTRS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName"}


def error_type(err: BaseException) -> str:
    return "TypeError" if isinstance(err, TypeError) else "Error"


def serialize_error(err: BaseException) -> dict:
    return {
        "type": error_type(err),
        "message": str(err),
        "stack": "".join(traceback.format_exception(err)).rstrip(),
    }


class PinoFormatter(logging.Formatter):
    """Renders records as the pino JSON lines the Node.js service wrote to stdout."""

    def __init__(self):
        super().__init__()
        self._pid = os.getpid()
        self._hostname = socket.gethostname()

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "level": _LEVELS.get(record.levelno, record.levelname.lower()),
            "time": int(record.created * 1000),
            "pid": self._pid,
            "hostname": self._hostname,
            "service.name": os.environ.get("OTEL_SERVICE_NAME"),
        }
        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            entry["trace_id"] = format(span_context.trace_id, "032x")
            entry["span_id"] = format(span_context.span_id, "016x")
            entry["trace_flags"] = format(span_context.trace_flags, "02x")
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("otel"):
                entry[key] = value
        message = record.getMessage()
        if message:
            entry["msg"] = message
        return json.dumps({k: v for k, v in entry.items() if v is not None}, default=str)


def get_logger() -> logging.Logger:
    logger = logging.getLogger("payment")
    if not any(isinstance(h.formatter, PinoFormatter) for h in logger.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(PinoFormatter())
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return logger
