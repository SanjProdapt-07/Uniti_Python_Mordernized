# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import logging
import os
import signal
import sys
import threading
from concurrent import futures

import demo_pb2_grpc
import grpc
import httpx
from grpc_health.v1 import health, health_pb2_grpc
from openfeature import api

from . import kafka, rpcmetrics
from .service import CheckoutService

logger = logging.getLogger("checkout")

GRACEFUL_STOP_SECONDS = 30
MAX_WORKERS = 64


class ConfigError(RuntimeError):
    pass


def must_map_env(key):
    value = os.environ.get(key, "")
    if value == "":
        raise ConfigError(f'environment variable "{key}" not set')
    return value


def configure_logging():
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    root.addHandler(handler)
    for noisy in ("httpx", "httpcore", "grpc", "openfeature"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def init_feature_flags():
    try:
        from openfeature.contrib.provider.flagd import FlagdProvider

        provider = FlagdProvider()
    except Exception as err:
        logger.error("Error creating flagd provider", extra={"error": str(err)})
        return
    try:
        api.set_provider(provider)
    except Exception as err:
        logger.error("Failed to set flagd as the provider", extra={"error": str(err)})
    try:
        from openfeature.contrib.hook.opentelemetry import TracingHook

        api.add_hooks([TracingHook()])
    except ImportError:
        pass


def build_service(client_interceptor):
    channels = []

    def channel(addr):
        ch = rpcmetrics.instrumented_channel(addr, client_interceptor)
        channels.append(ch)
        return ch

    shipping_addr = must_map_env("SHIPPING_ADDR")
    catalog = demo_pb2_grpc.ProductCatalogServiceStub(channel(must_map_env("PRODUCT_CATALOG_ADDR")))
    cart = demo_pb2_grpc.CartServiceStub(channel(must_map_env("CART_ADDR")))
    currency = demo_pb2_grpc.CurrencyServiceStub(channel(must_map_env("CURRENCY_ADDR")))
    email_addr = must_map_env("EMAIL_ADDR")
    payment = demo_pb2_grpc.PaymentServiceStub(channel(must_map_env("PAYMENT_ADDR")))

    kafka_addr = os.environ.get("KAFKA_ADDR", "")
    producer = None
    if kafka_addr != "":
        try:
            producer = kafka.create_kafka_producer([kafka_addr], logger)
        except Exception as err:
            logger.error(str(err))

    svc = CheckoutService(
        shipping_addr=shipping_addr,
        email_addr=email_addr,
        cart=cart,
        product_catalog=catalog,
        currency=currency,
        payment=payment,
        http_client=httpx.Client(timeout=None),
        kafka_broker_addr=kafka_addr,
        producer=producer,
        channel_factory=lambda addr: rpcmetrics.instrumented_channel(addr, client_interceptor),
    )
    return svc, channels


def describe(svc):
    fields = (
        ("productCatalogSvcAddr", os.environ.get("PRODUCT_CATALOG_ADDR", "")),
        ("cartSvcAddr", os.environ.get("CART_ADDR", "")),
        ("currencySvcAddr", os.environ.get("CURRENCY_ADDR", "")),
        ("shippingSvcAddr", svc.shipping_svc_addr),
        ("emailSvcAddr", svc.email_svc_addr),
        ("paymentSvcAddr", os.environ.get("PAYMENT_ADDR", "")),
        ("kafkaBrokerSvcAddr", svc.kafka_broker_svc_addr),
    )
    return "&{" + " ".join(f"{k}:{v}" for k, v in fields) + "}"


def create_server(svc, port, max_workers=MAX_WORKERS):
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=max_workers),
        interceptors=[rpcmetrics.ServerMetricsInterceptor()],
    )
    demo_pb2_grpc.add_CheckoutServiceServicer_to_server(svc, server)
    health_pb2_grpc.add_HealthServicer_to_server(health.HealthServicer(), server)
    address = f"[::]:{port}"
    bound_port = server.add_insecure_port(address)
    return server, address, bound_port


def main():
    configure_logging()
    port = must_map_env("CHECKOUT_PORT")

    init_feature_flags()

    svc, channels = build_service(rpcmetrics.ClientMetricsInterceptor())
    logger.info(f"service config: {describe(svc)}")

    server, address, _ = create_server(svc, port)
    logger.info(f'starting to listen on tcp: "{address}"')

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    server.start()
    stop.wait()

    server.stop(GRACEFUL_STOP_SECONDS).wait()
    logger.info("Checkout gRPC server stopped")

    if svc.kafka_producer_client is not None:
        svc.kafka_producer_client.close()
    svc.http_client.close()
    for ch in channels:
        ch.close()
    api.shutdown()


if __name__ == "__main__":
    main()
