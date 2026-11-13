#!/usr/bin/python
# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

tracer = trace.get_tracer("flagd_ui")


def set_route_attributes(plug: str, action: str) -> None:
    trace.get_current_span().set_attributes({"phoenix.plug": plug, "phoenix.action": action})


@contextmanager
def live_span(name: str) -> Iterator[None]:
    with tracer.start_as_current_span(
        name, kind=SpanKind.SERVER, record_exception=False, set_status_on_exception=False
    ) as span:
        try:
            yield
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, ""))
            raise
