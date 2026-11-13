# Accounting Service

This service consumes new orders from a Kafka topic and records them in the
`accounting` schema of PostgreSQL.

It subscribes to `KAFKA_TOPIC` (default `orders`) on `KAFKA_ADDR` as consumer
group `accounting`. Each `OrderResult` is written to `accounting."order"`,
`accounting.orderitem` and `accounting.shipping` in a single transaction. When
`DB_CONNECTION_STRING` (Npgsql format, e.g.
`Host=...;Username=...;Password=...;Database=...`) is not set, orders are only
logged.

## Tests

From the repo root:

```sh
docker run --rm -v $PWD:/repo -w /repo/src/accounting uniti-py314-test \
  sh -c "mkdir -p /tmp/gen && python -m grpc_tools.protoc -I/repo/pb --python_out=/tmp/gen /repo/pb/demo.proto && PYTHONPATH=/tmp/gen pytest -q"
```

## Docker Build

From the root directory, run:

```sh
docker compose build accounting
```

## Bump dependencies

Edit `requirements.in`, then regenerate `requirements.txt`:

```sh
pip-compile --allow-unsafe --generate-hashes --no-emit-index-url --output-file=requirements.txt --strip-extras requirements.in
```
