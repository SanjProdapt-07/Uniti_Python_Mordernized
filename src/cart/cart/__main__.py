# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
import logging
import os
import signal
import sys
import threading
import uuid

from .telemetry import prepare_environment


def _listen_port() -> str:
    for key in ("CART_PORT", "ASPNETCORE_HTTP_PORTS"):
        value = os.environ.get(key, "").split(";")[0].strip()
        if value:
            return value
    return "8080"


def main() -> None:
    valkey_address = os.environ.get("VALKEY_ADDR")
    if not valkey_address:
        print("VALKEY_ADDR environment variable is required.")
        sys.exit(1)
    valkey_password = os.environ.get("VALKEY_PASSWORD")
    valkey_tls = os.environ.get("VALKEY_TLS", "").strip().lower() == "true"

    service_instance_id = prepare_environment()

    from opentelemetry.instrumentation import auto_instrumentation

    auto_instrumentation.initialize()

    from opentelemetry import trace

    from .featureflags import setup_feature_flags
    from .opamp import OPAMP_SERVER_ENDPOINT_ENV, OPAMP_SERVER_TLS_INSECURE_SKIP_VERIFY_ENV, start_opamp_client, \
        stop_opamp_client
    from .server import build_server
    from .store import ValkeyCartStore
    from .telemetry import GrpcSpanNameProcessor, configure_logging

    configure_logging()
    logger = logging.getLogger("Program")

    tracer_provider = trace.get_tracer_provider()
    if hasattr(tracer_provider, "add_span_processor"):
        tracer_provider.add_span_processor(GrpcSpanNameProcessor())

    feature_client = setup_feature_flags()

    cart_store = ValkeyCartStore(valkey_address, feature_client, valkey_password, valkey_tls)
    bad_cart_store = ValkeyCartStore("badhost:1234", feature_client, connect_retry=1)

    opamp_agent = None
    opamp_endpoint = os.environ.get(OPAMP_SERVER_ENDPOINT_ENV)
    if opamp_endpoint:
        try:
            resource = getattr(tracer_provider, "resource", None)
            opamp_agent = start_opamp_client(
                opamp_endpoint,
                uuid.UUID(service_instance_id).bytes,
                resource.attributes if resource is not None else {},
                os.environ.get(OPAMP_SERVER_TLS_INSECURE_SKIP_VERIFY_ENV, ""),
            )
        except Exception:
            logger.warning("Failed to start OpAMP client", exc_info=True)
            opamp_agent = None

    cart_store.initialize()

    server, _ = build_server(cart_store, bad_cart_store, feature_client, f"[::]:{_listen_port()}")
    server.start()

    stopped = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopped.set())
    stopped.wait()
    server.stop(grace=5).wait()
    stop_opamp_client(opamp_agent, logger)


if __name__ == "__main__":
    main()
