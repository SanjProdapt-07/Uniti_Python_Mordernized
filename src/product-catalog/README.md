# Product Catalog Service

A Python 3.14 gRPC service (`grpcio`) that serves the product catalog from the
`catalog.products` table in PostgreSQL using psycopg 3 and a `psycopg_pool`
connection pool.

## Configuration

| Variable | Purpose |
|---|---|
| `PRODUCT_CATALOG_PORT` | gRPC listen port |
| `DB_CONNECTION_STRING` | PostgreSQL connection URI (required) |
| `FLAGD_HOST`, `FLAGD_PORT` | flagd provider |
| `OPAMP_SERVER_ENDPOINT` | Enables the OpAMP client (WebSocket) |
| `OPAMP_SERVER_TLS_INSECURE_SKIP_VERIFY` | Skip TLS verification for OpAMP |

## Database Calls Instrumentation

PostgreSQL queries are instrumented with the OpenTelemetry psycopg
instrumentation, with SQLCommenter enabled to append trace context to SQL
statements (for example, `traceparent` key-value pairs in query comments).
`OTEL_SEMCONV_STABILITY_OPT_IN=database` enables the stable database semantic
conventions, including the `db.client.operation.duration` metric.

## Feature flags

* `productCatalogFailure` fails `GetProduct`, evaluated with the `product_id`
  attribute in the evaluation context.
* `productCatalogLockContention` is checked every 10 seconds; while it is on,
  the service holds an `ACCESS EXCLUSIVE` lock on `catalog.products` for 30
  seconds at a time.

## Run tests

From the repository root:

```sh
docker run --rm -v $PWD:/repo -w /repo/src/product-catalog uniti-py314-test sh -c \
  "mkdir -p /tmp/gen && python -m grpc_tools.protoc -I/repo/pb --python_out=/tmp/gen --grpc_python_out=/tmp/gen /repo/pb/demo.proto && pip install -q --user --require-hashes -r requirements.txt && PYTHONPATH=/tmp/gen:. pytest -q"
```

## Docker Build

From the root directory, run:

```sh
docker compose build product-catalog
```
