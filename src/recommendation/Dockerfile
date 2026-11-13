# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /usr/src/app

COPY src/recommendation/requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY pb/demo.proto /tmp/pb/demo.proto
RUN python -m grpc_tools.protoc -I/tmp/pb --python_out=. --grpc_python_out=. /tmp/pb/demo.proto \
    && rm -rf /tmp/pb

COPY src/recommendation/recommendation ./recommendation

RUN useradd --system --uid 10001 --no-create-home app
USER 10001

ARG RECOMMENDATION_PORT=9001
EXPOSE ${RECOMMENDATION_PORT}
ENTRYPOINT ["python", "-m", "recommendation"]
