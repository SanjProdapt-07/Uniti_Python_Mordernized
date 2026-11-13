# Frontend service

The frontend is composed of two layers.

1. Client side application, written in React with Next.js (pages router). It is
   built as a static export (`output: 'export'`) and renders the components for
   the OTEL webstore. It keeps its browser OpenTelemetry tracer and the flagd
   web provider.
2. Server, written in Python with FastAPI (`frontend_server/`). It exposes the
   REST endpoints under `/api/*` that connect the client to the backend
   services (gRPC and HTTP), serves the exported client, and injects
   `window.ENV` into every HTML page per request.

| Path | Served by |
|---|---|
| `/`, `/cart`, `/product/{productId}`, `/cart/checkout/{orderId}` | exported HTML shell with `window.ENV` |
| `/api/cart`, `/api/checkout`, `/api/currency`, `/api/data`, `/api/recommendations`, `/api/shipping`, `/api/products`, `/api/products/{productId}` | Python API routes |
| `/_next/static/*`, `/icons/*`, `/favicon.ico` | static files from the export |

When a request carries the `synthetic_request=true` baggage entry, the browser
is told to send its traces straight to the collector
(`http://$OTEL_COLLECTOR_HOST:4318/v1/traces`) instead of
`PUBLIC_OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`.

## Build Locally

By running `docker compose up` at the root of the project you'll have access to
the frontend client by going to <http://localhost:8080/>.

## Local development

Build the client with `npm ci && npm run build` (output in `out/`), generate the
gRPC stubs, and start the server:

```shell
python -m grpc_tools.protoc -I ../../pb --python_out=. --grpc_python_out=. ../../pb/demo.proto
pip install --require-hashes -r requirements.txt
PORT=8080 python -m frontend_server
```

`FRONTEND_STATIC_DIR` overrides the location of the exported client.

## Tests

```shell
docker run --rm -v $(pwd)/../..:/repo -w /repo/src/frontend uniti-py314-test \
  sh -c "mkdir -p /tmp/gen && python -m grpc_tools.protoc -I/repo/pb --python_out=/tmp/gen --grpc_python_out=/tmp/gen /repo/pb/demo.proto && PYTHONPATH=/tmp/gen pytest -q -p no:cacheprovider"
```

The Cypress end-to-end suite (`cypress/`) runs against the full stack with
`make run-frontend-tests`.
