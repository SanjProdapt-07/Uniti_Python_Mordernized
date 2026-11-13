# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

FROM docker.io/library/python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f AS base

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
