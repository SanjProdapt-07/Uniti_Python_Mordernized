# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0


FROM docker.io/library/python:3.15-slim AS base

FROM base AS builder

WORKDIR /usr/src/app/

COPY ./src/checkout/requirements.txt ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --require-hashes -r requirements.txt

COPY ./pb/demo.proto ./pb/demo.proto
RUN mkdir -p /opt/gen \
    && /opt/venv/bin/python -m grpc_tools.protoc -I./pb --python_out=/opt/gen --grpc_python_out=/opt/gen ./pb/demo.proto \
    && /opt/venv/bin/pip uninstall -y grpcio-tools

FROM ghcr.io/grpc-ecosystem/grpc-health-probe:v0.4.62@sha256:1a900fcfc1b8e680511670908b47d9bc3f8464f88d9159f961c5d55c254b8140 AS grpc-health-probe

FROM base

WORKDIR /usr/src/app/

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/gen/ ./
COPY ./src/checkout/checkout/ ./checkout/
COPY --from=grpc-health-probe /ko-app/grpc-health-probe /bin/grpc_health_probe

RUN useradd --system --uid 65532 --no-create-home checkout
USER 65532

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED=true \
    OTEL_PYTHON_GRPC_EXCLUDED_SERVICES=grpc.health.v1.Health,flagd.evaluation.v1.Service,flagd.evaluation.v2.Service,flagd.sync.v1.FlagSyncService \
    OTEL_EXPERIMENTAL_RESOURCE_DETECTORS=otel,process,os,host,containerid

ARG CHECKOUT_PORT=5050
EXPOSE ${CHECKOUT_PORT}
ENTRYPOINT ["opentelemetry-instrument", "python", "-m", "checkout"]
