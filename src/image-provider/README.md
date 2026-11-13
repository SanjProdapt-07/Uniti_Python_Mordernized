# Image provider

Serves the product and banner images in `static/` for the Astronomy Shop. The
frontend-proxy routes `/images/*` here with the `/images` prefix stripped.

It is a small ASGI app on uvicorn that replaces the former nginx server and
keeps its behaviour:

- every file under `static/` is served at its path (GET, HEAD), with
  `Content-Type`, `Content-Length`, `Last-Modified`, `ETag` and
  `Accept-Ranges` as nginx sent them; conditional (`If-None-Match`,
  `If-Modified-Since`, `If-Match`, `If-Unmodified-Since`) and `Range`
  requests follow nginx semantics; `*.gz` siblings are served to clients that
  accept gzip;
- a directory without a trailing slash redirects (301); a directory without
  `index.html` is 403; anything else missing is 404 with the nginx error page;
- `/status` (prefix match) returns the nginx `stub_status` text that the
  collector's `nginx` receiver scrapes;
- every request produces one SERVER span named `image-provider`, continuing an
  incoming W3C `traceparent`, exported over OTLP/gRPC to
  `OTEL_COLLECTOR_HOST:OTEL_COLLECTOR_PORT_GRPC` (or the standard
  `OTEL_EXPORTER_OTLP_*` endpoint when set);
- an access log line in nginx `combined` format is written to stdout.

Configuration: `IMAGE_PROVIDER_PORT` (default 8081), `OTEL_SERVICE_NAME`,
`OTEL_RESOURCE_ATTRIBUTES`, `OTEL_COLLECTOR_HOST`, `OTEL_COLLECTOR_PORT_GRPC`.

## Tests

```sh
docker run --rm -v $PWD:/repo -w /repo/src/image-provider uniti-py314-test python -m pytest
```
