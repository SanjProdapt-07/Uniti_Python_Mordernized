# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

FROM ghcr.io/grpc-ecosystem/grpc-health-probe:v0.4.62@sha256:1a900fcfc1b8e680511670908b47d9bc3f8464f88d9159f961c5d55c254b8140 AS grpc-health-probe

FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /usr/src/app

COPY src/product-catalog/requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY pb/demo.proto /tmp/pb/demo.proto
RUN python -m grpc_tools.protoc -I/tmp/pb --python_out=. --grpc_python_out=. /tmp/pb/demo.proto \
    && rm -rf /tmp/pb

COPY src/product-catalog/product_catalog ./product_catalog
COPY --from=grpc-health-probe /ko-app/grpc-health-probe /bin/grpc_health_probe

RUN useradd --system --uid 10001 --no-create-home app
USER 10001

ARG PRODUCT_CATALOG_PORT=3550
EXPOSE ${PRODUCT_CATALOG_PORT}
ENTRYPOINT ["python", "-m", "product_catalog"]
