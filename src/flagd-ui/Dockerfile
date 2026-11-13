# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

FROM docker.io/library/python:3.15-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY ./src/flagd-ui/requirements.txt ./requirements.txt
RUN pip install --require-hashes -r requirements.txt

FROM base AS test

RUN pip install pytest==9.2.0 httpx==0.28.1

COPY ./src/flagd/demo.flagd.json /src/flagd/demo.flagd.json
COPY ./src/flagd-ui/pytest.ini /src/flagd-ui/pytest.ini
COPY ./src/flagd-ui/flagd_ui /src/flagd-ui/flagd_ui
COPY ./src/flagd-ui/tests /src/flagd-ui/tests

RUN cd /src/flagd-ui \
  && PYTHONPATH=. python -m pytest -q -p no:cacheprovider \
  && touch /tmp/flagd-ui-tests-passed

FROM base AS final

# BuildKit skips unused stages, so depend on the test marker to require tests before building the image.
COPY --from=test /tmp/flagd-ui-tests-passed /tmp/flagd-ui-tests-passed

ARG FLAGD_UI_UID=1000
ARG FLAGD_UI_GID=1000
RUN groupadd --gid ${FLAGD_UI_GID} flagdui \
  && useradd --uid ${FLAGD_UI_UID} --gid ${FLAGD_UI_GID} --no-create-home --shell /usr/sbin/nologin flagdui \
  && mkdir -p /app/data \
  && chown flagdui:flagdui /app/data

COPY ./src/flagd-ui/flagd_ui ./flagd_ui

ENV OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf \
    OTEL_TRACES_EXPORTER=otlp \
    OTEL_METRICS_EXPORTER=none \
    OTEL_LOGS_EXPORTER=none \
    OTEL_PYTHON_DISABLED_INSTRUMENTATIONS=fastapi

USER flagdui

ARG FLAGD_UI_PORT=4000
EXPOSE ${FLAGD_UI_PORT}

CMD ["opentelemetry-instrument", "python", "-m", "flagd_ui"]
