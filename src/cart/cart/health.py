# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
import logging

import grpc
from grpc_health.v1 import health_pb2, health_pb2_grpc

logger = logging.getLogger("cart.healthcheck.HealthServiceImpl")

CART_SERVICE_NAME = "oteldemo.CartService"


class ReadinessCheck:
    def __init__(self, feature_client):
        self._feature_client = feature_client

    def check_health(self) -> bool:
        return not self._feature_client.get_boolean_value("failedReadinessProbe", False)


class HealthService(health_pb2_grpc.HealthServicer):
    def __init__(self, checks: dict):
        self._checks = checks

    def _status(self, healthy: bool):
        return health_pb2.HealthCheckResponse.SERVING if healthy else health_pb2.HealthCheckResponse.NOT_SERVING

    def Check(self, request, context):
        logger.info(
            f"Received health check request for service: {request.service}",
            extra={"event.name": "cart.health_check.request", "service": request.service},
        )
        return self._check(request, context)

    def _check(self, request, context):
        if not request.service:
            healthy = all(check.check_health() for check in self._checks.values())
            return health_pb2.HealthCheckResponse(status=self._status(healthy))
        check = self._checks.get(request.service)
        if check is None:
            context.abort(grpc.StatusCode.UNKNOWN, "Exception was thrown by handler.")
        return health_pb2.HealthCheckResponse(status=self._status(check.check_health()))

    def Watch(self, request, context):
        logger.info(
            f"Received health watch request for service: {request.service}",
            extra={"event.name": "cart.health_watch.request", "service": request.service},
        )
        yield self.Check(request, context)
