# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import os
import socket

import uvicorn

from .app import ImageProvider
from .status import Stats, counting_protocol
from .telemetry import configure_tracing

STATIC_ROOT = "/static"
KEEPALIVE_TIMEOUT = 75


def listen_sockets(port: int) -> list[socket.socket]:
    sockets = []
    for family, address in ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")):
        try:
            sock = socket.socket(family, socket.SOCK_STREAM)
        except OSError:
            continue
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family == socket.AF_INET6:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        try:
            sock.bind((address, port))
        except OSError:
            sock.close()
            if family == socket.AF_INET:
                raise
            continue
        sock.set_inheritable(True)
        sockets.append(sock)
    return sockets


def build_server(app: ImageProvider) -> uvicorn.Server:
    config = uvicorn.Config(
        app,
        http=counting_protocol(app.stats),
        lifespan="off",
        proxy_headers=False,
        server_header=False,
        access_log=False,
        timeout_keep_alive=KEEPALIVE_TIMEOUT,
    )
    return uvicorn.Server(config)


def main() -> None:
    port = int(os.environ.get("IMAGE_PROVIDER_PORT", "8081"))
    provider = configure_tracing()
    app = ImageProvider(STATIC_ROOT, Stats(), tracer_provider=provider)
    try:
        build_server(app).run(sockets=listen_sockets(port))
    finally:
        if provider is not None:
            provider.shutdown()


if __name__ == "__main__":
    main()
