# Python modernization of the OpenTelemetry Demo

Source: open-telemetry/opentelemetry-demo at the commit in `ORIGINAL_COMMIT`.
Target: **Python 3.14** for every service the demo itself implements. Behaviour,
contracts and telemetry must be indistinguishable from the original.

## Scope

| Component | Original | Python target |
|---|---|---|
| ad | Java 21 / grpc-java | grpcio server |
| recommendation | Java 21 / Spring Boot 4.1 | grpcio server + client (product-catalog) |
| cart | .NET 10 + Valkey | grpcio server + redis-py |
| checkout | Go (6 services + Kafka producer) | grpcio server/clients + httpx + confluent-kafka producer |
| currency | C++ | grpcio server |
| payment | Node.js | grpcio server |
| product-catalog | Go + Postgres | grpcio server + psycopg 3 |
| email | Ruby / Sinatra | FastAPI + Jinja2 |
| quote | PHP / Slim | FastAPI |
| shipping | Rust / actix-web | FastAPI + httpx (quote) |
| accounting | .NET 10 (EF Core) + Postgres | confluent-kafka consumer + psycopg 3 / SQLAlchemy 2 |
| fraud-detection | Kotlin | confluent-kafka consumer |
| frontend (server side) | Next.js API routes + SSR | FastAPI; serves the React client bundle |
| flagd-ui | Elixir / Phoenix LiveView | FastAPI + Jinja2 + HTMX |
| image-provider | nginx | FastAPI static files |
| load-generator, agent, chatbot, mcp, shared, telemetry-docs | Python 3.14/3.15 | Python 3.14 |

Unchanged: the frontend's browser code (React, browser OTel, flagd web provider),
react-native-app, frontend-proxy (Envoy), opamp-server, Kafka, Postgres, Valkey,
flagd, otel-collector, Jaeger, Grafana, Prometheus, OpenSearch.

## Conventions for every migrated service

- **Location:** in place, `src/<service>/`. The original-language sources are
  removed once the Python version is complete, so compose paths stay valid.
- **Layout:** `src/<service>/` holds `Dockerfile`, `requirements.in`,
  `requirements.txt` (pinned), `pyproject.toml` (`requires-python = ">=3.14"`),
  the package `<service_pkg>/` (entry point `<service_pkg>/__main__.py`),
  `tests/test_*.py`, `README.md`.
- **Contracts are frozen:** `pb/demo.proto` is not edited. gRPC stubs are generated
  at build time with `python -m grpc_tools.protoc` into top-level `demo_pb2` /
  `demo_pb2_grpc`. HTTP method, path, request and response bodies, status codes
  and Kafka topic/payload stay byte-compatible.
- **Configuration:** same environment variable names, same defaults, same port.
- **Feature flags:** OpenFeature Python SDK + flagd provider (`FLAGD_HOST`,
  `FLAGD_PORT`), same flag keys, types, defaults and evaluation points.
- **Telemetry:** same `OTEL_SERVICE_NAME`. Spans, span names, attributes, events,
  metrics and log messages must match the original code and
  `telemetry-schema/` (services, attributes, metrics). Use
  `opentelemetry-instrument` auto-instrumentation plus manual spans and metrics
  where the original creates them. OTLP export is driven by the standard
  `OTEL_*` variables that compose already sets.
- **gRPC servers** also register the standard health service
  (`grpcio-health-checking`).
- **Container:** `FROM python:3.14-slim`, build context is the repo root (as in
  compose). Run as a non-root user.
- **Tests:** pytest, no network or real Valkey/Postgres/Kafka/flagd; fakes behind
  small interfaces. Run with the shared image:
  `docker run --rm -v $PWD:/repo -w /repo/src/<service> uniti-py314-test ...`
- **Comments:** only where strictly necessary (see `AGENTS.md`).

## Acceptance

1. Unit tests pass in Python 3.14.
2. `test/telemetry` (traces, edges, metrics, logs) passes against the full stack.
3. The frontend Cypress suite and the load generator run unchanged.
4. Every flagd fault-injection scenario still triggers in the same way.
