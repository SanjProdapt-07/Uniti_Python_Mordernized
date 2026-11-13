# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from image_provider.app import ImageProvider
from image_provider.status import Stats

STATIC = Path(__file__).resolve().parent.parent / "static"


@dataclass
class Result:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""


def call(
    app,
    method: str,
    raw_path: bytes,
    headers: dict[str, str] | None = None,
    query: bytes = b"",
    server=("image-provider", 8081),
    client=("10.0.0.7", 51234),
) -> Result:
    headers = {"host": "image-provider:8081", **(headers or {})}
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": raw_path.decode("latin-1"),
        "raw_path": raw_path,
        "query_string": query,
        "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
        "server": server,
        "client": client,
    }
    result = Result(0)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            result.status = message["status"]
            result.headers = {k.decode(): v.decode() for k, v in message["headers"]}
        else:
            result.body += message.get("body", b"")

    asyncio.run(app(scope, receive, send))
    return result


@pytest.fixture
def exporter():
    return InMemorySpanExporter()


@pytest.fixture
def provider(exporter):
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    return tracer_provider


@pytest.fixture
def app(provider):
    return ImageProvider(STATIC, Stats(), tracer_provider=provider, access_log=False)


@pytest.fixture
def tmp_app(tmp_path, provider):
    return ImageProvider(tmp_path, Stats(), tracer_provider=provider, access_log=False)
