# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import logging
import os
import signal
import sys
import threading
from concurrent import futures

import grpc
import prometheus_client
from grpc_health.v1 import health, health_pb2, health_pb2_grpc
from opentelemetry import trace

import demo_pb2_grpc

logger = logging.getLogger("oteldemo.AdService")


class Log4jStyleFormatter(logging.Formatter):
    def __init__(self):
        super().__init__("%(asctime)s - %(name)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    def format(self, record):
        line = super().format(record)
        ctx = trace.get_current_span().get_span_context()
        if ctx.is_valid:
            ids = f"trace_id={ctx.trace_id:032x} span_id={ctx.span_id:016x} trace_flags={ctx.trace_flags:02x}"
        else:
            ids = "trace_id= span_id= trace_flags="
        return f"{line} {ids} "


def configure_logging() -> None:
    root = logging.getLogger("oteldemo")
    if not any(isinstance(h.formatter, Log4jStyleFormatter) for h in root.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(Log4jStyleFormatter())
        root.addHandler(handler)
    root.setLevel(logging.INFO)


def build_server(service, port: int, max_workers: int = 32):
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_workers))
    demo_pb2_grpc.add_AdServiceServicer_to_server(service, server)
    health_servicer = health.HealthServicer()
    health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)
    try:
        bound = server.add_insecure_port(f"[::]:{port}")
    except RuntimeError:
        bound = 0
    if not bound:
        bound = server.add_insecure_port(f"0.0.0.0:{port}")
    return server, health_servicer, bound


def main() -> int:
    configure_logging()
    logger.info("Ad service starting.")

    port_env = os.environ.get("AD_PORT")
    if port_env is None:
        raise RuntimeError("environment vars: AD_PORT must not be null")
    port = int(port_env)
    prometheus_port = int(os.environ.get("AD_PROMETHEUS_PORT", "9465"))

    from ad.service import PROMETHEUS_REGISTRY, AdService
    from ad.problempattern import CPULoad

    prometheus_client.disable_created_metrics()
    prometheus_server, _ = prometheus_client.start_http_server(prometheus_port, registry=PROMETHEUS_REGISTRY)
    logger.info(f"Prometheus metrics endpoint started, listening on {prometheus_server.server_port}/metrics")

    from openfeature import api
    from openfeature.contrib.provider.flagd import FlagdProvider

    api.set_provider(FlagdProvider())

    server, health_servicer, _ = build_server(AdService(), port)
    server.start()
    logger.info(f"Ad service started, listening on {port}")
    health_servicer.set("", health_pb2.HealthCheckResponse.SERVING)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    stop.wait()

    print("*** shutting down gRPC ads server since JVM is shutting down", file=sys.stderr)
    health_servicer.enter_graceful_shutdown()
    server.stop(None).wait()
    prometheus_server.shutdown()
    CPULoad.get_instance().execute(False)
    api.shutdown()
    print("*** server shut down", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
