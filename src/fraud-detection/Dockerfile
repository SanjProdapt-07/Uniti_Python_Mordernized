# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

FROM docker.io/library/python:3.14-slim AS builder

WORKDIR /build
COPY ./src/fraud-detection/requirements.txt .
RUN pip install --no-cache-dir --prefix=/reqs --require-hashes -r requirements.txt

COPY ./pb/demo.proto ./pb/demo.proto
RUN PYTHONPATH=/reqs/lib/python3.14/site-packages \
    python -m grpc_tools.protoc -I./pb --python_out=. ./pb/demo.proto

# -----------------------------------------------------------------------------

FROM docker.io/library/python:3.14-slim

RUN useradd --system --uid 10001 --no-create-home app

WORKDIR /usr/src/app
COPY --from=builder /reqs /usr/local
COPY --from=builder /build/demo_pb2.py .
COPY ./src/fraud-detection/fraud_detection ./fraud_detection

ENV PYTHONUNBUFFERED=1 \
    OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf \
    OTEL_TRACES_EXPORTER=otlp \
    OTEL_METRICS_EXPORTER=otlp \
    OTEL_LOGS_EXPORTER=otlp

USER app
ENTRYPOINT ["opentelemetry-instrument", "python", "-m", "fraud_detection"]
