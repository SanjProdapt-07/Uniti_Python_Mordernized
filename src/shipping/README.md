# Shipping Service

The Shipping service queries `quote` for a price quote, provides tracking IDs,
and the impression of order fulfillment & shipping processes. It is a Python
(FastAPI) service that calls `quote` over HTTP with httpx.

## Build

From `../../`, run:

```sh
docker compose build shipping
```

## Test

```sh
PYTHONPATH=. pytest
```

## Feature Flags

* `intlShippingSlowdown`: integer flag (default 0). When non-zero, non-US
  shipping requests are delayed by the flag's value in seconds to simulate
  overseas shipping latency. US addresses are never affected.

## Environment Variables

| Variable        | Default             | Description                       |
|-----------------|---------------------|-----------------------------------|
| `SHIPPING_PORT` | (required)          | Listening port                    |
| `QUOTE_ADDR`    | `http://quote:8090` | Base URL of the quote service     |
| `FLAGD_HOST`    | `localhost`         | Hostname of the flagd service     |
| `FLAGD_PORT`    | `8013`              | Port of the flagd evaluation API  |
| `IPV6_ENABLED`  | unset               | `true` binds to `[::]`            |
