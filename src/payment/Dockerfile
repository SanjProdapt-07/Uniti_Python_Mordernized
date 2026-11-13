# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

FROM docker.io/library/python:3.14-slim AS base

FROM base AS builder
COPY ./src/payment/requirements.txt .
RUN pip install --no-cache-dir --prefix=/reqs --require-hashes -r requirements.txt
COPY ./pb/demo.proto /proto/demo.proto
RUN mkdir /gen && PYTHONPATH=/reqs/lib/python3.14/site-packages python -m grpc_tools.protoc \
      -I/proto --python_out=/gen --grpc_python_out=/gen /proto/demo.proto

FROM base
COPY --from=builder /reqs /usr/local
WORKDIR /usr/src/app/
COPY --from=builder /gen/ ./
COPY ./src/payment/payment/ payment/
RUN useradd --system --uid 10001 --no-create-home app
USER 10001

ARG PAYMENT_PORT=50051
EXPOSE ${PAYMENT_PORT}
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["opentelemetry-instrument", "python", "-m", "payment"]
