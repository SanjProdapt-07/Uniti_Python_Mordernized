# Ad Service

The Ad service provides advertisement based on context keys. If no context keys
are provided then it returns random ads. It is a Python 3.14 gRPC service
(`oteldemo.AdService`) that also serves the standard gRPC health service.

## Running

```sh
export AD_PORT=9555
export FLAGD_HOST=flagd FLAGD_PORT=8013
opentelemetry-instrument python -m ad
```

From the repository root: `docker compose build ad`.

## Tests

`prometheus-client` is not part of the shared test image, so install it first:

```sh
docker run --rm -v $PWD:/repo -w /repo/src/ad uniti-py314-test sh -c \
  "pip install -q --user prometheus-client && mkdir -p /tmp/gen && \
   python -m grpc_tools.protoc -I/repo/pb --python_out=/tmp/gen --grpc_python_out=/tmp/gen \
   /repo/pb/demo.proto && PYTHONPATH=/tmp/gen:. pytest -q"
```

## Custom metrics: bridging Prometheus to OpenTelemetry

The Ad service intentionally emits custom metrics in **two different ways**:

1. **OpenTelemetry SDK** (recommended): `demo.ad.requests` is created with the
   OpenTelemetry metrics API and exported to the OTel Collector over OTLP.
2. **Prometheus client library**: `demo_ad_served_total{category}` is created
   with `prometheus-client` and exposed on a separate HTTP endpoint
   (`AD_PROMETHEUS_PORT`, default `9465`). The OTel Collector's `prometheus/ad`
   receiver scrapes this endpoint and forwards the metric into the same metrics
   pipeline.

The Prometheus-client path demonstrates a **common pattern during
OpenTelemetry adoption**: organizations frequently already own Prometheus
instrumentation and want to ingest it into an OpenTelemetry-native pipeline
without rewriting everything up front. The Collector's `prometheus` receiver is
the bridge that makes this possible.

**Recommendation**: this is a *transitional* pattern. Prefer the OpenTelemetry
SDK for new custom metrics and migrate existing Prometheus-client metrics to it.

## Fault injection

* `adHighCpu`: four busy-looping worker processes are started while the flag is
  on (separate interpreters, so they load four cores despite the GIL).
* `adManualGc`: each request fills memory with cyclic garbage (up to 90% of a
  200 MiB budget or 500000 objects) and runs ten timed full `gc.collect()`
  passes, adding seconds of latency and a memory spike.
* `adFailure`: one request in ten fails with `UNAVAILABLE`.
