# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0


FROM docker.io/library/python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY ./src/quote/requirements.txt requirements.txt
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY ./src/quote/quote_service/ quote_service/

RUN useradd --system --no-create-home --uid 10001 app
USER 10001

ENV OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf \
    OTEL_PYTHON_DISABLED_INSTRUMENTATIONS=fastapi \
    OTEL_SEMCONV_STABILITY_OPT_IN=http \
    OTEL_EXPERIMENTAL_RESOURCE_DETECTORS=otel,containerid

ARG QUOTE_PORT=8090
EXPOSE ${QUOTE_PORT}

CMD ["opentelemetry-instrument", "python", "-m", "quote_service"]
