# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

from concurrent import futures
from decimal import ROUND_HALF_UP, Decimal

import grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

import demo_pb2
import demo_pb2_grpc
from payment import charge as charge_module
from payment.logger import error_type, get_logger, serialize_error

logger = get_logger()


def format_amount(units: int, nanos: int) -> str:
    value = float(units) + nanos / 1000000000
    return str(Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


class PaymentService(demo_pb2_grpc.PaymentServiceServicer):
    def __init__(self, flag_client=None):
        self.flag_client = flag_client

    def Charge(self, request, context):
        span = trace.get_current_span()
        try:
            if not request.HasField("amount"):
                raise TypeError("Cannot read properties of null (reading 'units')")
            amount = request.amount
            span.set_attributes({"demo.payment.amount": format_amount(amount.units, amount.nanos)})
            logger.info("Charge request received.")

            transaction_id = charge_module.charge(request, self.flag_client)
            return demo_pb2.ChargeResponse(transaction_id=transaction_id)
        except Exception as err:
            logger.warning("", extra={"err": serialize_error(err)})
            span.set_status(Status(StatusCode.ERROR, str(err)))
            span.set_attribute("error.type", error_type(err))
            context.abort(grpc.StatusCode.UNKNOWN, str(err))


def build_server(service: PaymentService, address: str, max_workers: int = 16) -> tuple[grpc.Server, int]:
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_workers))
    health_servicer = health.HealthServicer()
    health_servicer.set("", health_pb2.HealthCheckResponse.SERVING)
    health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)
    demo_pb2_grpc.add_PaymentServiceServicer_to_server(service, server)
    port = server.add_insecure_port(address)
    return server, port
