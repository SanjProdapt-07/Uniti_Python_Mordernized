# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
"""rpc.server.duration / rpc.client.duration, which otelgrpc recorded and opentelemetry-instrumentation-grpc does not."""

import time

import grpc
from opentelemetry import metrics

_HEALTH_PREFIX = "/grpc.health.v1.Health/"


def _split(full_method):
    service, _, method = full_method.lstrip("/").partition("/")
    return service, method


def _attributes(full_method, code):
    service, method = _split(full_method)
    return {
        "rpc.system": "grpc",
        "rpc.service": service,
        "rpc.method": method,
        "rpc.grpc.status_code": code.value[0],
    }


def _meter():
    return metrics.get_meter("checkout")


class ServerMetricsInterceptor(grpc.ServerInterceptor):
    def __init__(self):
        self._duration = _meter().create_histogram(
            "rpc.server.duration", unit="ms", description="Measures the duration of inbound RPC."
        )

    def intercept_service(self, continuation, handler_call_details):
        handler = continuation(handler_call_details)
        method = handler_call_details.method
        if handler is None or handler.unary_unary is None or method.startswith(_HEALTH_PREFIX):
            return handler
        behavior = handler.unary_unary
        duration = self._duration

        def timed(request, context):
            start = time.perf_counter()
            code = grpc.StatusCode.OK
            try:
                return behavior(request, context)
            except Exception:
                code = grpc.StatusCode.UNKNOWN
                raise
            finally:
                set_code = context.code() if hasattr(context, "code") else None
                if isinstance(set_code, grpc.StatusCode):
                    code = set_code
                duration.record((time.perf_counter() - start) * 1000, _attributes(method, code))

        return grpc.unary_unary_rpc_method_handler(
            timed,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )


class ClientMetricsInterceptor(grpc.UnaryUnaryClientInterceptor):
    def __init__(self):
        self._duration = _meter().create_histogram(
            "rpc.client.duration", unit="ms", description="Measures the duration of outbound RPC."
        )

    def intercept_unary_unary(self, continuation, client_call_details, request):
        start = time.perf_counter()
        call = continuation(client_call_details, request)
        try:
            code = call.code()
        except Exception:
            code = grpc.StatusCode.UNKNOWN
        self._duration.record(
            (time.perf_counter() - start) * 1000, _attributes(client_call_details.method, code)
        )
        return call


def instrumented_channel(target, interceptor):
    return grpc.intercept_channel(grpc.insecure_channel(target), interceptor)
