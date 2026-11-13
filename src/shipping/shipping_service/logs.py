# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import logging
import time

from opentelemetry._logs import SeverityNumber, get_logger

_LEVELS = {
    "INFO": (SeverityNumber.INFO, logging.INFO),
    "WARN": (SeverityNumber.WARN, logging.WARNING),
}

_stdout = logging.getLogger("shipping.stdout")


def emit(target: str, level: str, body: str | None = None, event_name: str | None = None, **attributes) -> None:
    severity_number, std_level = _LEVELS[level]
    get_logger(target).emit(
        timestamp=time.time_ns(),
        severity_number=severity_number,
        severity_text=level,
        body=body,
        attributes=attributes or None,
        event_name=event_name,
    )
    fields = " ".join(f"{k}={v}" for k, v in attributes.items())
    _stdout.log(std_level, "%s: %s", target, " ".join(p for p in (event_name, body, fields) if p))
