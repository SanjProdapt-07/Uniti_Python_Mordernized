# Email Service

The Email service "sends" an email to the customer with their order details by
rendering `email_service/views/confirmation.html` (Jinja2) and delivering it to
an in-memory test mailer. It expects a JSON payload like:

```json
{
  "email": "some.address@website.com",
  "order": "<order result as JSON>"
}
```

The `emailMemoryLeak` flag pads the body and stops clearing delivered messages,
which makes memory grow on every request.

## Running locally

```sh
pip install -r requirements.txt
EMAIL_PORT=6060 opentelemetry-instrument python -m email_service
```

## Test

```sh
PYTHONPATH=. pytest
```

## Docker Build

From the repository root, run `docker compose build email`.
