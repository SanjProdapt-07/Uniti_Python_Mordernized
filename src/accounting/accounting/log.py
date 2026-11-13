# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import logging
import sys

from accounting.helpers import to_diagnostic_string

CATEGORY = "Accounting.Consumer"
LIFETIME_CATEGORY = "Microsoft.Hosting.Lifetime"
EVENT_NAME = "otel.event.name"

logger = logging.getLogger(CATEGORY)
lifetime_logger = logging.getLogger(LIFETIME_CATEGORY)

_LEVELS = {
    logging.DEBUG: "dbug",
    logging.INFO: "info",
    logging.WARNING: "warn",
    logging.ERROR: "fail",
    logging.CRITICAL: "crit",
}
_PADDING = " " * 6


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = render(record)
        lines = [f"{_LEVELS.get(record.levelno, 'info')}: {record.name}[0]", _PADDING + message]
        if record.exc_info:
            lines.extend(_PADDING + line for line in self.formatException(record.exc_info).splitlines())
        return "\n".join(lines)


def render(record: logging.LogRecord) -> str:
    if record.args or not isinstance(record.msg, str):
        return record.getMessage()
    try:
        return record.msg.format_map(record.__dict__)
    except (KeyError, IndexError, ValueError):
        return record.msg


def configure_console(stream=None) -> None:
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(ConsoleFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def _log(target: logging.Logger, level: int, template: str, event: str | None = None, exc=None, **values) -> None:
    extra = dict(values)
    if event is not None:
        extra[EVENT_NAME] = event
    target.log(level, template, exc_info=exc, extra=extra)


def order_received_message(order) -> None:
    if logger.isEnabledFor(logging.INFO):
        _log(
            logger,
            logging.INFO,
            "Order details: {@OrderResult}.",
            "accounting.order.received",
            **{"@OrderResult": to_diagnostic_string(order)},
        )


def kafka_connecting(servers: str) -> None:
    _log(logger, logging.INFO, "Connecting to Kafka: {servers}", "accounting.kafka.connecting", servers=servers)


def consume_error(exception: BaseException, reason: str) -> None:
    _log(logger, logging.ERROR, "Consume error: {reason}", "accounting.kafka.consume_failed", exception, reason=reason)


def consumer_closing() -> None:
    _log(logger, logging.INFO, "Closing consumer", "accounting.consumer.closing")


def duplicate_order_skipped() -> None:
    _log(logger, logging.INFO, "Duplicate order received, skipping.", "accounting.order.duplicate_skipped")


def order_parsing_failed(exception: BaseException) -> None:
    _log(logger, logging.ERROR, "Order parsing failed:", "accounting.order.parsing_failed", exception)


def application_started(content_root: str, environment: str = "Production") -> None:
    _log(lifetime_logger, logging.INFO, "Application started. Press Ctrl+C to shut down.")
    _log(lifetime_logger, logging.INFO, "Hosting environment: {EnvName}", EnvName=environment)
    _log(lifetime_logger, logging.INFO, "Content root path: {ContentRoot}", ContentRoot=content_root)


def application_stopping() -> None:
    _log(lifetime_logger, logging.INFO, "Application is shutting down...")
