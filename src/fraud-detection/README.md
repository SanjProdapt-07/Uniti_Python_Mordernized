# Fraud Detection Service

This service receives new orders by a Kafka topic and returns cases which are
suspected of fraud.

It subscribes to `KAFKA_TOPIC` (default `orders`) on `KAFKA_ADDR` as consumer
group `fraud-detection`, and evaluates the `kafkaQueueProblems` and
`kafkaConsumerDead` flags through flagd (`FLAGD_HOST`, `FLAGD_PORT`).

## Tests

From the repo root:

```sh
docker run --rm -v $PWD:/repo -w /repo/src/fraud-detection uniti-py314-test \
  sh -c "mkdir -p /tmp/gen && python -m grpc_tools.protoc -I/repo/pb --python_out=/tmp/gen /repo/pb/demo.proto && PYTHONPATH=/tmp/gen pytest -q"
```

## Docker Build

From the root directory, run:

```sh
docker compose build fraud-detection
```

## Bump dependencies

Edit `requirements.in`, then regenerate `requirements.txt`:

```sh
pip-compile --allow-unsafe --generate-hashes --no-emit-index-url --output-file=requirements.txt --strip-extras requirements.in
```
