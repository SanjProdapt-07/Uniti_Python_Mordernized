# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import time
from pathlib import Path

from fastapi import FastAPI, Request, Response
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from opentelemetry import metrics, trace
from opentelemetry._logs import get_logger

from . import ostruct
from .flags import FlagEvaluator
from .mailer import TestMailer

HTML = "text/html;charset=utf-8"


def _ruby_to_s(value):
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    return value


def _each(value):
    if not isinstance(value, list):
        raise TypeError(f"undefined method 'each' for {type(value).__name__}")
    return value


def build_templates() -> Environment:
    env = Environment(
        loader=FileSystemLoader(Path(__file__).parent / "views"),
        autoescape=False,
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        finalize=_ruby_to_s,
    )
    env.filters["each"] = _each
    return env


def create_app(flags: FlagEvaluator, mailer: TestMailer | None = None) -> FastAPI:
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    mailer = mailer or TestMailer()
    app.state.mailer = mailer
    templates = build_templates()
    tracer = trace.get_tracer("email")
    render_tracer = trace.get_tracer("OpenTelemetry::Instrumentation::Sinatra")
    logger = get_logger("email")
    confirmation_counter = metrics.get_meter("email").create_counter(
        "demo.notification.confirmations",
        unit="1",
        description="Counts the number of order confirmation emails sent",
    )

    def render(template: str, **context) -> str:
        with render_tracer.start_as_current_span(
            "sinatra.render_template", attributes={"sinatra.template_name": template}
        ):
            return templates.get_template(f"{template}.html").render(**context)

    def send_email(data) -> None:
        with tracer.start_as_current_span("send_email") as span:
            memory_leak_multiplier = flags.number("emailMemoryLeak", 0)

            confirmation_content = render("confirmation", order=data.order)
            whitespace_length = max(0, len(confirmation_content) * (memory_leak_multiplier - 1))

            mailer.mail(
                to=data.email,
                from_="noreply@example.com",
                subject="Your confirmation email",
                body=confirmation_content + " " * whitespace_length,
            )

            if memory_leak_multiplier < 1:
                mailer.deliveries.clear()

            order_id = data.order.order_id
            if order_id is not None:
                span.set_attribute("demo.order.id", order_id)
            logger.emit(
                timestamp=time.time_ns(),
                severity_text="INFO",
                body="Order confirmation email sent",
                attributes={"demo.order.id": order_id} if order_id is not None else None,
                event_name="email.confirmation_sent",
            )
            print(f"Order confirmation email sent for order {_ruby_to_s(order_id)}", flush=True)

    @app.post("/send_order_confirmation")
    async def send_order_confirmation(request: Request) -> Response:
        current_span = trace.get_current_span()
        try:
            data = ostruct.loads(await request.body())
            order_id = data.order.order_id
            if order_id is not None:
                current_span.set_attributes({"demo.order.id": order_id})
            confirmation_counter.add(1)
            send_email(data)
        except Exception as exc:
            current_span.record_exception(exc)
            return Response(status_code=500, media_type=HTML)
        return Response(status_code=200, media_type=HTML)

    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except ImportError:
        return app
    FastAPIInstrumentor.instrument_app(app, exclude_spans=["receive", "send"])
    return app
