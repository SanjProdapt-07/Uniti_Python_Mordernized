# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

FROM docker.io/library/python:3.15-slim AS base

FROM base AS builder
COPY ./src/ad/requirements.txt .
RUN pip install --no-cache-dir --prefix=/reqs --require-hashes -r requirements.txt
COPY ./pb/demo.proto /proto/demo.proto
RUN mkdir /gen && PYTHONPATH=/reqs/lib/python3.14/site-packages python -m grpc_tools.protoc \
      -I/proto --python_out=/gen --grpc_python_out=/gen /proto/demo.proto

FROM base
COPY --from=builder /reqs /usr/local
WORKDIR /usr/src/app/
COPY --from=builder /gen/ ./
COPY ./src/ad/ad/ ad/
RUN useradd --system --uid 10001 --no-create-home app
USER 10001

ARG AD_PORT=9555
EXPOSE ${AD_PORT}
ARG AD_PROMETHEUS_PORT=9465
EXPOSE ${AD_PROMETHEUS_PORT}
ENV PYTHONUNBUFFERED=1 OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf

ENTRYPOINT ["opentelemetry-instrument", "python", "-m", "ad"]
