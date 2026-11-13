# Cart Service

This service stores user shopping carts in Valkey. It is a Python 3.14 gRPC
service (`grpcio`) that uses `redis-py` to talk to Valkey.

Each cart is stored as a Valkey hash keyed by the user ID, with a single field
`cart` holding the serialized `oteldemo.Cart` protobuf message. Keys expire after
60 minutes. This layout is unchanged from the original .NET service, so existing
data stays readable.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `VALKEY_ADDR` | required | `host:port` of Valkey |
| `VALKEY_PASSWORD` | unset | Valkey password |
| `VALKEY_TLS` | `false` | Connect to Valkey over TLS |
| `CART_PORT` | `ASPNETCORE_HTTP_PORTS`, then `8080` | gRPC listen port |
| `FLAGD_HOST`, `FLAGD_PORT` | `localhost`, `8013` | flagd provider |
| `OPAMP_SERVER_ENDPOINT` | unset | Enables the OpAMP client (WebSocket) |
| `OPAMP_SERVER_TLS_INSECURE_SKIP_VERIFY` | `false` | Skip TLS verification for OpAMP |

## Run tests

From the repository root:

```sh
docker run --rm -v $PWD:/repo -w /repo/src/cart uniti-py314-test sh -c \
  "mkdir -p /tmp/gen && python -m grpc_tools.protoc -I/repo/pb --python_out=/tmp/gen --grpc_python_out=/tmp/gen /repo/pb/demo.proto && pip install -q --user --require-hashes -r requirements.txt && PYTHONPATH=/tmp/gen:. pytest -q"
```

## Docker Build

From the root directory of this repository, run:

```sh
docker compose build cart
```

## Metric cardinality overflow

Enable `cartMetricCardinalityOverflow` in the feature flag UI to deliberately add
the current trace ID as `demo.cart.trace_id` on the get-cart latency metric.
After enough distinct traces exceed the SDK limit (2,000 by default), view the
**Metric cardinality overflow** section of the **Self-Observability** dashboard.
This simulates an incorrect attempt to correlate metrics with traces; use exemplars
instead (the cart service already enables them). The flag is off by default.

Turning the flag off stops adding trace IDs, but cumulative overflow can remain
visible. Restart cart with the flag off to reset SDK state; historical overflow
remains visible in dashboard time ranges that include it.
