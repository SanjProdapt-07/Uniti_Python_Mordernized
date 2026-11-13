# Checkout Service

This service provides checkout services for the application. It orchestrates
`PlaceOrder`: it reads the cart, prices the items, quotes and books shipping,
charges the card, empties the cart, sends the confirmation email and publishes
the `OrderResult` to Kafka (only when `KAFKA_ADDR` is set).

## Docker Build

From the root directory, run:

```sh
docker compose build checkout
```

The gRPC stubs (`demo_pb2`, `demo_pb2_grpc`) are generated from `pb/demo.proto`
during the image build.

## Tests

From the root directory, run:

```sh
docker run --rm -v $PWD:/repo -w /repo/src/checkout uniti-py314-test sh -c \
  "mkdir -p /tmp/gen && python -m grpc_tools.protoc -I/repo/pb --python_out=/tmp/gen \
   --grpc_python_out=/tmp/gen /repo/pb/demo.proto && PYTHONPATH=/tmp/gen:. pytest -q -p no:cacheprovider"
```

## Feature flags

`flags.json` lists the flags this service evaluates (`emitRawPii`,
`kafkaQueueProblems`, `paymentUnreachable`); `checkout/flags.py` holds the
typed accessors.

## Bump dependencies

Edit `requirements.in`, then regenerate the pinned file with pip-tools:

```sh
pip-compile --allow-unsafe --generate-hashes --no-emit-index-url \
  --output-file=requirements.txt --strip-extras requirements.in
```
