# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

FROM ghcr.io/grpc-ecosystem/grpc-health-probe:v0.4.61@sha256:9df7b8dfbaf01d20eaedcd6de084c557074046f87cfc0413b7939ae18087c249 AS grpc-health-probe

FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /usr/src/app

COPY src/cart/requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY pb/demo.proto /tmp/pb/demo.proto
RUN python -m grpc_tools.protoc -I/tmp/pb --python_out=. --grpc_python_out=. /tmp/pb/demo.proto \
    && rm -rf /tmp/pb

COPY src/cart/cart ./cart
COPY --from=grpc-health-probe /ko-app/grpc-health-probe /bin/grpc_health_probe

RUN useradd --system --uid 10001 --no-create-home app
USER 10001

ARG CART_PORT=7070
EXPOSE ${CART_PORT}
ENTRYPOINT ["python", "-m", "cart"]
