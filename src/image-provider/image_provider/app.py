# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import os
import sys
import time
import traceback
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import SpanKind, StatusCode, TracerProvider
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from .static_files import Reply, StaticFiles, error_reply, normalize_uri
from .status import Stats
from .telemetry import SCOPE_NAME, SPAN_NAME

STATUS_LOCATION = b"/status"
STATUS_METHODS = {"GET", "HEAD"}

_propagator = TraceContextTextMapPropagator()


def _flavor(http_version: str) -> str:
    return http_version if "." in http_version else f"{http_version}.0"


def _access_log(scope: dict[str, Any], headers: dict[str, str], status: int, sent: int) -> str:
    client = scope.get("client")
    target = scope.get("raw_path", scope["path"].encode()).decode("latin-1")
    if scope.get("query_string"):
        target += "?" + scope["query_string"].decode("latin-1")
    request = f"{scope['method']} {target} HTTP/{scope.get('http_version', '1.1')}"
    stamp = time.strftime("%d/%b/%Y:%H:%M:%S %z", time.localtime())
    return (
        f'{client[0] if client else "-"} - - [{stamp}] "{request}" {status} {sent} '
        f'"{headers.get("referer", "-")}" "{headers.get("user-agent", "-")}"'
    )


class ImageProvider:
    def __init__(
        self,
        root: str | os.PathLike[str],
        stats: Stats | None = None,
        tracer_provider: TracerProvider | None = None,
        access_log: bool = True,
    ):
        self.static = StaticFiles(root)
        self.stats = stats or Stats()
        self.tracer = trace.get_tracer(SCOPE_NAME, tracer_provider=tracer_provider)
        self.access_log = access_log

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return

        self.stats.requests += 1
        self.stats.writing += 1
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        span = self.tracer.start_span(
            SPAN_NAME,
            context=_propagator.extract(headers),
            kind=SpanKind.SERVER,
        )
        route = None
        sent = 0
        try:
            try:
                reply, route = self._dispatch(scope, headers)
            except Exception as exc:
                span.record_exception(exc)
                traceback.print_exc()
                reply = error_reply(500)
            sent = await self._send(scope, send, reply)
        finally:
            self.stats.writing -= 1
            self._finish_span(span, scope, headers, reply.status, route, sent)
            if self.access_log:
                print(_access_log(scope, headers, reply.status, sent), file=sys.stdout, flush=True)

    def _dispatch(self, scope: dict[str, Any], headers: dict[str, str]) -> tuple[Reply, str | None]:
        method = scope["method"]
        uri = normalize_uri(scope.get("raw_path") or scope["path"].encode())
        if uri is None:
            return error_reply(400), None
        if uri.startswith(STATUS_LOCATION):
            if method not in STATUS_METHODS:
                return error_reply(405), STATUS_LOCATION.decode()
            body = self.stats.render()
            reply = Reply(
                200,
                [("content-type", "text/plain"), ("content-length", str(len(body)))],
                lambda: iter((body,)),
                len(body),
            )
            return reply, STATUS_LOCATION.decode()
        server = scope.get("server")
        port = server[1] if server else None
        return self.static.handle(method, uri, scope.get("query_string", b""), headers, port), None

    @staticmethod
    async def _send(scope: dict[str, Any], send: Any, reply: Reply) -> int:
        await send(
            {
                "type": "http.response.start",
                "status": reply.status,
                "headers": [(k.encode("latin-1"), v.encode("latin-1")) for k, v in reply.headers],
            }
        )
        sent = 0
        if scope["method"] != "HEAD":
            for chunk in reply.body():
                sent += len(chunk)
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})
        return sent

    @staticmethod
    def _finish_span(
        span: trace.Span,
        scope: dict[str, Any],
        headers: dict[str, str],
        status: int,
        route: str | None,
        sent: int,
    ) -> None:
        target = scope.get("raw_path", scope["path"].encode()).decode("latin-1")
        if scope.get("query_string"):
            target += "?" + scope["query_string"].decode("latin-1")
        attributes: dict[str, Any] = {
            "http.method": scope["method"],
            "http.target": target,
            "http.scheme": scope.get("scheme", "http"),
            "http.flavor": _flavor(scope.get("http_version", "1.1")),
        }
        if route:
            attributes["http.route"] = route
        if "user-agent" in headers:
            attributes["http.user_agent"] = headers["user-agent"]
        try:
            received = int(headers.get("content-length", "0"))
        except ValueError:
            received = 0
        if received > 0:
            attributes["http.request_content_length"] = received
        if sent > 0:
            attributes["http.response_content_length"] = sent
        attributes["http.status_code"] = status
        host = headers.get("host", "").strip().lower()
        host_name = host.rsplit(":", 1)[0] if ":" in host and not host.endswith("]") else host
        if host_name:
            attributes["net.host.name"] = host_name
        server = scope.get("server")
        if server and server[1]:
            attributes["net.host.port"] = server[1]
        client = scope.get("client")
        if client:
            attributes["net.sock.peer.addr"] = client[0]
            attributes["net.sock.peer.port"] = client[1]
        span.set_attributes(attributes)
        if status >= 500:
            span.set_status(StatusCode.ERROR)
        span.end()
