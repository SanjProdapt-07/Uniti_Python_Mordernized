# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

FROM docker.io/library/node:26.13.0-slim@sha256:5adad64f9a1fca2d51972caa65983634c8a774eb300d72b20f7f2a0f60368072 AS client-builder

WORKDIR /app

COPY ./src/frontend/package.json package.json
COPY ./src/frontend/package-lock.json package-lock.json

RUN npm ci

COPY ./src/frontend/components/ components/
COPY ./src/frontend/gateways/ gateways/
COPY ./src/frontend/pages/ pages/
COPY ./src/frontend/protos/ protos/
COPY ./src/frontend/providers/ providers/
COPY ./src/frontend/public/ public/
COPY ./src/frontend/styles/ styles/
COPY ./src/frontend/types/ types/

COPY ./src/frontend/utils/enums/ utils/enums/
COPY ./src/frontend/utils/telemetry/ utils/telemetry/
COPY ./src/frontend/utils/imageLoader.js utils/imageLoader.js
COPY ./src/frontend/utils/Request.ts utils/Request.ts

COPY ./src/frontend/next.config.js next.config.js
COPY ./src/frontend/tsconfig.json tsconfig.json

RUN npm run build

# -----------------------------------------------------------------------------

FROM docker.io/library/python:3.15-slim AS server-builder

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY ./src/frontend/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes --no-deps -r /tmp/requirements.txt

COPY ./pb/demo.proto /tmp/pb/demo.proto
RUN mkdir -p /opt/gen \
    && python -m grpc_tools.protoc -I/tmp/pb --python_out=/opt/gen --grpc_python_out=/opt/gen /tmp/pb/demo.proto \
    && pip uninstall --yes grpcio-tools

# -----------------------------------------------------------------------------

FROM docker.io/library/python:3.15-slim

RUN useradd --system --uid 10001 --no-create-home frontend

WORKDIR /app

COPY --from=server-builder /opt/venv /opt/venv
COPY --from=server-builder /opt/gen/ ./
COPY --from=client-builder /app/out/ out/
COPY ./src/frontend/frontend_server/ frontend_server/

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OTEL_TRACES_EXPORTER=otlp \
    OTEL_METRICS_EXPORTER=otlp \
    OTEL_LOGS_EXPORTER=otlp \
    OTEL_EXPORTER_OTLP_PROTOCOL=grpc \
    OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED=true \
    OTEL_PYTHON_DISABLED_INSTRUMENTATIONS=fastapi \
    OTEL_EXPERIMENTAL_RESOURCE_DETECTORS=containerid,otel,host,os,process,aws_ec2,aws_eks,gcp

USER frontend

ARG FRONTEND_PORT=8080
EXPOSE ${FRONTEND_PORT}

CMD ["opentelemetry-instrument", "python", "-m", "frontend_server"]
