# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
import logging
import os
import signal
import sys
import threading

from .telemetry import prepare_environment, prepare_opamp_identity


def _init_database(logger):
    from psycopg_pool import ConnectionPool

    conn_str = os.environ.get("DB_CONNECTION_STRING", "")
    if not conn_str:
        raise RuntimeError("DB_CONNECTION_STRING environment variable not set")
    try:
        pool = ConnectionPool(conn_str, min_size=1, max_size=20, timeout=60, open=False, name="product-catalog")
    except Exception as ex:
        raise RuntimeError(f"failed to open database connection: {ex}") from ex
    try:
        pool.open(wait=True, timeout=30)
        with pool.connection() as conn:
            conn.execute("SELECT 1")
    except Exception as ex:
        pool.close()
        raise RuntimeError(f"failed to ping database: {ex}") from ex
    logger.info("Database connection established")
    return pool


def main() -> None:
    from .opamp import OPAMP_SERVER_ENDPOINT_ENV, OPAMP_SERVER_TLS_INSECURE_SKIP_VERIFY_ENV

    opamp_identity = prepare_opamp_identity()
    prepare_environment()

    disabled = [d for d in os.environ.get("OTEL_PYTHON_DISABLED_INSTRUMENTATIONS", "").split(",") if d.strip()]
    os.environ["OTEL_PYTHON_DISABLED_INSTRUMENTATIONS"] = ",".join(disabled + ["psycopg"])

    from opentelemetry.instrumentation import auto_instrumentation

    auto_instrumentation.initialize()

    from openfeature import api
    from openfeature.contrib.hook.opentelemetry import TracingHook
    from openfeature.contrib.provider.flagd import FlagdProvider
    from opentelemetry import trace
    from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor

    from .catalog import PostgresCatalog
    from .metrics import register_pool_metrics
    from .opamp import start_opamp_client, stop_opamp_client
    from .service import LockContentionLoop, build_server
    from .telemetry import GrpcSpanNameProcessor, configure_logging

    configure_logging()
    logger = logging.getLogger("product-catalog")

    tracer_provider = trace.get_tracer_provider()
    if hasattr(tracer_provider, "add_span_processor"):
        tracer_provider.add_span_processor(GrpcSpanNameProcessor())

    PsycopgInstrumentor().instrument(enable_commenter=True, skip_dep_check=True)

    try:
        pool = _init_database(logger)
    except Exception as ex:
        logger.error(f"Error initializing database: {ex}")
        sys.exit(1)
    register_pool_metrics(pool)

    try:
        api.add_hooks([TracingHook()])
        api.set_provider(FlagdProvider())
    except Exception as ex:
        logger.error("Error creating flagd provider", extra={"error": str(ex)})
    feature_client = api.get_client()

    catalog = PostgresCatalog(pool)
    lock_loop = LockContentionLoop(catalog, feature_client)
    lock_loop.start()

    opamp_agent = None
    endpoint = os.environ.get(OPAMP_SERVER_ENDPOINT_ENV)
    if endpoint and opamp_identity is not None:
        try:
            resource = getattr(tracer_provider, "resource", None)
            opamp_agent = start_opamp_client(
                endpoint, opamp_identity, resource.attributes if resource is not None else {},
                os.environ.get(OPAMP_SERVER_TLS_INSECURE_SKIP_VERIFY_ENV, ""),
            )
        except Exception as ex:
            logger.error(f"Failed to start OpAMP client: {ex}")

    port = os.environ.get("PRODUCT_CATALOG_PORT")
    if port is None:
        logger.error('Environment Variable Not Set: "PRODUCT_CATALOG_PORT"')
        port = ""
    logger.info(f"Product Catalog gRPC server started on port: {port}")
    server, _ = build_server(catalog, feature_client, f"[::]:{port or 0}")
    server.start()

    stopped = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopped.set())
    stopped.wait()

    server.stop(grace=30).wait()
    logger.info("Product Catalog gRPC server stopped")
    lock_loop.stop()
    stop_opamp_client(opamp_agent, logger)
    api.shutdown()
    pool.close()
    logger.info("Database connection closed")


if __name__ == "__main__":
    main()
