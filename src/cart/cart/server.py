# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
from concurrent import futures

import grpc
from grpc_health.v1 import health_pb2_grpc

import demo_pb2_grpc

from .health import CART_SERVICE_NAME, HealthService, ReadinessCheck
from .service import CartService
from .telemetry import HttpServerMetricsInterceptor


def build_server(cart_store, bad_cart_store, feature_client, address: str, max_workers: int = 32,
                 interceptors=None):
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=max_workers),
        interceptors=interceptors if interceptors is not None else [HttpServerMetricsInterceptor()],
    )
    demo_pb2_grpc.add_CartServiceServicer_to_server(
        CartService(cart_store, bad_cart_store, feature_client), server)
    health_pb2_grpc.add_HealthServicer_to_server(
        HealthService({CART_SERVICE_NAME: ReadinessCheck(feature_client)}), server)
    port = server.add_insecure_port(address)
    return server, port
