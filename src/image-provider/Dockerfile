# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

FROM docker.io/library/python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS base

FROM base AS builder
COPY ./src/image-provider/requirements.txt .
RUN pip install --no-cache-dir --prefix="/reqs" --require-hashes -r requirements.txt

FROM base
RUN apt-get -qq update \
    && apt-get install -y --no-install-recommends wget \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin image-provider

COPY --from=builder /reqs /usr/local

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

COPY src/image-provider/static/ /static/
COPY src/image-provider/image_provider/ image_provider/

USER image-provider

ARG IMAGE_PROVIDER_PORT=8081
EXPOSE ${IMAGE_PROVIDER_PORT}

CMD ["python", "-m", "image_provider"]
