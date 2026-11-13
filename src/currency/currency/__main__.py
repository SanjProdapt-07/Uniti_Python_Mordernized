# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import os
import signal
import sys
import threading
from concurrent import futures

import grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc
from opentelemetry._logs import SeverityNumber

import demo_pb2_grpc
from currency.service import CurrencyService, log_event

SERVER_SHUTDOWN_GRACE_SECONDS = 4


def build_server(service: CurrencyService, address: str, max_workers: int = 16) -> tuple[grpc.Server, int]:
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_workers))
    demo_pb2_grpc.add_CurrencyServiceServicer_to_server(service, server)
    health_servicer = health.HealthServicer()
    health_servicer.set("", health_pb2.HealthCheckResponse.SERVING)
    health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)
    port = server.add_insecure_port(address)
    return server, port


def main(argv: list[str]) -> int:
    port_arg = argv[1] if len(argv) > 1 else os.environ.get("CURRENCY_PORT")
    if port_arg is None:
        print("Usage: currency <port>", end="")
        return 0

    service = CurrencyService(version=os.environ.get("VERSION", "unknown"))
    logger = service.logger

    ip = "0.0.0.0"
    if os.environ.get("IPV6_ENABLED") == "true":
        ip = "[::]"
        log_event(
            logger,
            SeverityNumber.INFO,
            "currency.server.ip_overwrite",
            "Overwriting Localhost IP",
            {"server.address": ip},
        )
    address = f"{ip}:{int(port_arg)}"

    stop = threading.Event()
    received = {}

    def on_signal(signum, _frame):
        received["name"] = signal.Signals(signum).name
        stop.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    try:
        server, _ = build_server(service, address)
        server.start()
    except Exception:
        print("Failed to start Currency Server", file=sys.stderr)
        return 1

    log_event(
        logger,
        SeverityNumber.INFO,
        "currency.server.started",
        "Currency Server started",
        {"server.address": address},
    )

    stop.wait()
    message = f"Received {received['name']}, shutting down Currency Server"
    print(message, flush=True)
    log_event(logger, SeverityNumber.INFO, "currency.server.stopping", message)

    server.stop(SERVER_SHUTDOWN_GRACE_SECONDS).wait()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
