# Flagd-ui

This application provides a user interface for configuring the feature
flags of the flagd service.

This is a [FastAPI](https://fastapi.tiangolo.com/) application rendering
[Jinja2](https://jinja.palletsprojects.com/) templates. The pages stay live
through [htmx](https://htmx.org/) and its WebSocket extension: every page holds
one WebSocket (`/feature/live`) that carries the UI events to the server and
the re-rendered fragments back, so changes made by the scheduler, the API or
another browser show up immediately.

The UI is served under the `/feature` base path behind the frontend proxy.

## Running the application

The application can be run with the rest of the demo using the documented
[docker compose or make commands](https://opentelemetry.io/docs/demo/#running-the-demo).

## Local development

* Copy [../flagd/demo.flagd.json](../flagd/demo.flagd.json) to a scratch location.
* Start the server, pointing it at that file:
  `FLAGD_UI_STORAGE_FILE_PATH=/tmp/demo.flagd.json python -m flagd_ui`
* Run the tests with `PYTHONPATH=. pytest`.

Now you can visit `localhost:4000/feature` from your browser.

### Styles

`flagd_ui/static/assets/css/app.css` is compiled and committed, so running the
service needs no Node or Tailwind tooling. After changing classes in the
templates, regenerate it with the Tailwind v4.0.9 standalone CLI and the
heroicons v2.2.0 `optimized` directory:

```sh
HEROICONS_DIR=/path/to/heroicons/optimized tailwindcss \
  --input=assets/css/app.css --output=flagd_ui/static/assets/css/app.css --minify
```

## Scheduler

The `Scheduler` tab automates failure scenarios, so that an incident appears and
resolves on its own while the demo is left running. This is useful when
presenting a backend that shows issues at a "recent" point in time.

Each interval the scheduler activates up to a configured number of distinct
randomly picked flags. Every activation gets its own hold duration between the
configured minimum and maximum, and its own offset, so that the whole activation
fits inside the interval. When a hold expires that flag is set back to its
resting variant, the flag's own configured default, which is `off` for every
failure scenario.

The following can be configured:

* **Interval**: how often activations happen.
* **Minimum and maximum duration**: bounds on how long a flag stays active. A
  random value in between is picked for each activation. The maximum has to fit
  within the interval.
* **Flags at once**: how many distinct flags may be activated per interval. A
  flag is never picked twice in the same interval, and asking for more than the
  number of selected flags simply activates all of them.
* **Flags**: which flag variants may be picked, chosen one variant at a time. A
  flag with graded variants can take part with only some of them, so
  `cartFailure` can be scheduled at `10%` and `25%` while `100%` is left out.
  A flag is offered when it has a resting variant, its configured default, to
  return to and at least one other variant to switch to.

  A flag is picked first and one of its selected variants second, so a flag with
  many variants is no more likely to be chosen than a flag with one.
* **Seed**: makes the sequence of picks reproducible. When left empty a seed is
  generated and reported back, so a run can be repeated later.

Stopping the scheduler reverts any flags that are still being held active.

## Programmatic use through the API

This service exposes a REST API to ease its usage in a programmatic way for
power users.

You can read the current configuration using this HTTP call:

```json
$ curl localhost:8080/feature/api/read | jq

{
  "flags": {
    "adFailure": {
      "defaultVariant": "off",
      "description": "Fail ad service",
      "state": "ENABLED",
      "variants": {
        "off": false,
        "on": true
      }
    },
    "adHighCpu": {
      "defaultVariant": "off",
      "description": "Triggers high cpu load in the ad service",
      "state": "ENABLED",
      "variants": {
        "off": false,
        "on": true
      }
    },
    "adManualGc": {
      "defaultVariant": "off",
      "description": "Triggers full manual garbage collections in the ad service",
      "state": "ENABLED",
      "variants": {
        "off": false,
        "on": true
      }
    },
    ...
  }
}
```

You can also write a new settings file by sending a new configuration inside
the `data` field of a POST request body.

Bear in mind that _all_ the data will be rewritten by this write operation.

```sh
$ curl --header "Content-Type: application/json" \
  --request POST \
  --data '{"data": {"$schema":"https://flagd.dev/schema/v0/flags.json","flags":{"adFailure":{"defaultVariant":"on","description":"Fail ad service","state":"ENABLED","variants":{"off":false,"on":true}}...' \
  http://localhost:8080/feature/api/write
```

In addition to the `/read` and `/write` endpoint, we also offer these endpoint
to stay compatible with the old version of Flagd-ui:

* `/read-file` (`GET`)
* `/write-to-file` (`POST`)
