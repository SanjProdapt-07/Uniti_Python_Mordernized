# Quote Service

The Quote Service calculates the shipping costs, based on the number of items
to be shipped (`8.99` per item, rounded to two decimals).

It is a Python (FastAPI) service, using `opentelemetry-instrument`
auto-instrumentation plus manual instrumentation (`calculate-quote` span,
`quotes` counter, `Calculated quote` log).

## Build and run

```sh
docker compose build quote
docker compose up quote
```

Send a request:

```sh
curl --location 'http://localhost:8090/getquote' \
--header 'Content-Type: application/json' \
--data '{"numberOfItems":3}'
```

## Test

```sh
PYTHONPATH=. pytest
```
