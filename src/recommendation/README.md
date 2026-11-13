# Recommendation Service

This service provides recommendations for other products based on the currently
selected product. It is a Python 3.14 gRPC service (`grpcio`) that calls the
product catalog over gRPC.

Catalog calls have a two-second deadline, so a catalog that stops responding
does not hold recommendation requests open indefinitely. Set
`RECOMMENDATION_CATALOG_TIMEOUT_MS` to change this timeout in milliseconds.

## Configuration

| Variable | Default |
|---|---|
| `RECOMMENDATION_PORT` | `9001` |
| `PRODUCT_CATALOG_ADDR` | `localhost:3550` |
| `RECOMMENDATION_CATALOG_TIMEOUT_MS` | `2000` |
| `FLAGD_HOST`, `FLAGD_PORT` | `localhost`, `8013` |

The `recommendationCacheFailure` flag turns on a deliberately leaky cache: every
cache miss appends the catalog plus a quarter of the cache to itself.

## Run tests

From the repository root:

```sh
docker run --rm -v $PWD:/repo -w /repo/src/recommendation uniti-py314-test sh -c \
  "mkdir -p /tmp/gen && python -m grpc_tools.protoc -I/repo/pb --python_out=/tmp/gen --grpc_python_out=/tmp/gen /repo/pb/demo.proto && pip install -q --user --require-hashes -r requirements.txt && PYTHONPATH=/tmp/gen:. pytest -q"
```

## Docker Build

From the root directory, run:

```sh
docker compose build recommendation
```
