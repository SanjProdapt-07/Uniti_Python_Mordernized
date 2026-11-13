# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import threading

import grpc
import pytest
from grpc_health.v1 import health_pb2, health_pb2_grpc

from checkout import __main__ as entry
from checkout import rpcmetrics


def test_health_check_serving_and_graceful_stop(harness):
    stub = health_pb2_grpc.HealthStub(harness.channel)
    resp = stub.Check(health_pb2.HealthCheckRequest(), timeout=2)
    assert resp.status == health_pb2.HealthCheckResponse.SERVING

    done = threading.Event()
    harness.server.stop(5).wait()
    done.set()
    assert done.is_set()
    with pytest.raises(grpc.RpcError):
        stub.Check(health_pb2.HealthCheckRequest(), timeout=1)


@pytest.mark.parametrize("missing", [
    "SHIPPING_ADDR", "PRODUCT_CATALOG_ADDR", "CART_ADDR", "CURRENCY_ADDR", "EMAIL_ADDR", "PAYMENT_ADDR",
])
def test_required_env(monkeypatch, missing):
    for key, value in {
        "SHIPPING_ADDR": "http://shipping:50050",
        "PRODUCT_CATALOG_ADDR": "product-catalog:3550",
        "CART_ADDR": "cart:7070",
        "CURRENCY_ADDR": "currency:7001",
        "EMAIL_ADDR": "http://email:6060",
        "PAYMENT_ADDR": "payment:50051",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv(missing)
    with pytest.raises(entry.ConfigError, match=f'^environment variable "{missing}" not set$'):
        entry.build_service(rpcmetrics.ClientMetricsInterceptor())


def test_checkout_port_required(monkeypatch):
    monkeypatch.delenv("CHECKOUT_PORT", raising=False)
    with pytest.raises(entry.ConfigError, match='^environment variable "CHECKOUT_PORT" not set$'):
        entry.must_map_env("CHECKOUT_PORT")


def test_service_config_without_kafka(monkeypatch):
    for key, value in {
        "SHIPPING_ADDR": "http://shipping:50050",
        "PRODUCT_CATALOG_ADDR": "product-catalog:3550",
        "CART_ADDR": "cart:7070",
        "CURRENCY_ADDR": "currency:7001",
        "EMAIL_ADDR": "http://email:6060",
        "PAYMENT_ADDR": "payment:50051",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("KAFKA_ADDR", raising=False)
    svc, channels = entry.build_service(rpcmetrics.ClientMetricsInterceptor())
    try:
        assert svc.kafka_producer_client is None
        assert svc.kafka_broker_svc_addr == ""
        assert entry.describe(svc) == (
            "&{productCatalogSvcAddr:product-catalog:3550 cartSvcAddr:cart:7070 currencySvcAddr:currency:7001 "
            "shippingSvcAddr:http://shipping:50050 emailSvcAddr:http://email:6060 paymentSvcAddr:payment:50051 "
            "kafkaBrokerSvcAddr:}"
        )
    finally:
        for ch in channels:
            ch.close()
