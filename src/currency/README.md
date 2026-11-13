# Currency Service

The Currency Service converts money from one currency to another. It is a
Python 3.14 gRPC service (`oteldemo.CurrencyService`) that also serves the
standard gRPC health service.

## Running

```sh
docker compose build currency
docker compose up currency
```

The port is taken from the first command line argument or from `CURRENCY_PORT`.

## Tests

```sh
docker run --rm -v $PWD:/repo -w /repo/src/currency uniti-py314-test sh -c \
  "mkdir -p /tmp/gen && python -m grpc_tools.protoc -I/repo/pb --python_out=/tmp/gen \
   --grpc_python_out=/tmp/gen /repo/pb/demo.proto && PYTHONPATH=/tmp/gen:. pytest -q"
```

`tests/data/convert_golden.txt` holds conversion results produced by the
original C++ implementation; the tests check the Python maths against them.
