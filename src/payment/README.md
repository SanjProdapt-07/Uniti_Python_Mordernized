# Payment Service

This service is responsible for processing and validating payments through the
application. It is a Python 3.14 gRPC service (`oteldemo.PaymentService`) that
also serves the standard gRPC health service. Feature flags are read from flagd
through OpenFeature (`FLAGD_HOST`, `FLAGD_PORT`).

## Docker Build

From the root directory, run:

```sh
docker compose build payment
```

## Tests

```sh
docker run --rm -v $PWD:/repo -w /repo/src/payment uniti-py314-test sh -c \
  "mkdir -p /tmp/gen && python -m grpc_tools.protoc -I/repo/pb --python_out=/tmp/gen \
   --grpc_python_out=/tmp/gen /repo/pb/demo.proto && PYTHONPATH=/tmp/gen:. pytest -q"
```

`tests/data` holds card-validation and amount-formatting results produced by the
original Node.js libraries (`simple-card-validator` 1.1.0, `Number.toFixed`).
