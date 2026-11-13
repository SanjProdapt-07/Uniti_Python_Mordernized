#!/usr/bin/python
# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import FileResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocketDisconnect

from . import config
from . import storage as storage_mod
from .params import decode_pairs, flatten_values
from .pubsub import PubSub
from .scheduler import Scheduler
from .storage import Storage
from .telemetry import live_span, set_route_attributes
from .views import (
    VIEWS,
    AdvancedEditor,
    Dashboard,
    LiveView,
    SchedulerView,
    Services,
    field,
    flag_dom_id,
    format_ms,
    has_targeting,
    is_selected,
    next_trigger_label,
    selected_variant,
    variants_of,
)

logger = logging.getLogger(__name__)

PACKAGE_DIR = Path(__file__).parent
STATIC_DIR = PACKAGE_DIR / "static"
SESSION_COOKIE = "_flagd_ui_key"
LIVE_PLUG = "Elixir.Phoenix.LiveView.Plug"
FEATURE_CONTROLLER = "Elixir.FlagdUiWeb.FeatureController"

SECURE_BROWSER_HEADERS = {
    "referrer-policy": "strict-origin-when-cross-origin",
    "content-security-policy": "base-uri 'self'; frame-ancestors 'self';",
    "x-content-type-options": "nosniff",
    "x-frame-options": "SAMEORIGIN",
    "x-permitted-cross-domain-policies": "none",
}

STATUS_MESSAGES = {
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    406: "Not Acceptable",
    500: "Internal Server Error",
}

MIME_FORMATS = {"text/html": "html", "application/json": "json"}

# Collapses runs of slashes, e.g. "//api//read" -> "/api/read".
_SLASHES = re.compile(r"/{2,}")


def _accepted_format(accept: str | None, formats: list[str]) -> str | None:
    if not accept or not accept.strip():
        return formats[0]
    entries = []
    for index, raw in enumerate(accept.split(",")):
        parts = [part.strip() for part in raw.split(";")]
        mime, quality = parts[0].lower(), 1.0
        for param in parts[1:]:
            if param.startswith("q="):
                with contextlib.suppress(ValueError):
                    quality = float(param[2:])
        if mime and quality > 0:
            entries.append((-quality, index, mime))
    for _, _, mime in sorted(entries):
        if mime == "*/*":
            return formats[0]
        if mime.endswith("/*"):
            prefix = mime[:-1]
            for fmt in formats:
                if any(m.startswith(prefix) and f == fmt for m, f in MIME_FORMATS.items()):
                    return fmt
            continue
        fmt = MIME_FORMATS.get(mime)
        if fmt in formats:
            return fmt
    return None


def json_response(payload: Any, status: int = 200) -> Response:
    return Response(
        storage_mod.encode(payload),
        status_code=status,
        headers={"content-type": "application/json; charset=utf-8"},
    )


def error_response(status: int, fmt: str = "html") -> Response:
    message = STATUS_MESSAGES.get(status, "Internal Server Error")
    if fmt == "json":
        return json_response({"errors": {"detail": message}}, status)
    return Response(message, status_code=status, headers={"content-type": "text/html; charset=utf-8"})


def negotiated_error(request: Request, status: int) -> Response:
    fmt = _accepted_format(request.headers.get("accept"), ["html", "json"]) or "html"
    return error_response(status, fmt)


class PathNormalizer:
    """Serves the app under ROOT_PATH and tolerates the "//" left by Envoy's prefix rewrite."""

    def __init__(self, app: FastAPI) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] in ("http", "websocket"):
            path = _SLASHES.sub("/", scope["path"])
            root = config.ROOT_PATH
            if path == root or path.startswith(root + "/"):
                path = path[len(root) :] or "/"
            if path != scope["path"]:
                scope = {**scope, "path": path, "raw_path": path.encode()}
        await self.app(scope, receive, send)


def _static_versions() -> dict[str, str]:
    versions = {}
    for file in (STATIC_DIR / "assets").rglob("*"):
        if file.is_file():
            relative = "/" + file.relative_to(STATIC_DIR).as_posix()
            versions[relative] = hashlib.md5(file.read_bytes()).hexdigest()[:8]
    return versions


def _templates() -> Environment:
    env = Environment(
        loader=FileSystemLoader(PACKAGE_DIR / "templates"),
        autoescape=select_autoescape(["html"]),
        trim_blocks=False,
    )
    versions = _static_versions()

    def path(route: str) -> str:
        return config.ROOT_PATH if route == "/" else config.ROOT_PATH + route

    def static_path(asset: str) -> str:
        version = versions.get(asset)
        return path(asset) + (f"?vsn={version}" if version else "")

    env.globals.update(
        root_path=config.ROOT_PATH,
        path=path,
        static_path=static_path,
        variants_of=variants_of,
        selected_variant=selected_variant,
        has_targeting=has_targeting,
        field=field,
        flag_dom_id=flag_dom_id,
        format_ms=format_ms,
        next_trigger_label=next_trigger_label,
        is_selected=is_selected,
    )
    return env


async def _request_params(request: Request) -> dict[str, Any] | None:
    pairs: list[tuple[str, Any]] = list(request.query_params.multi_items())
    params = decode_pairs(pairs)
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type == "application/json" or content_type.endswith("+json"):
        body = await request.body()
        if body.strip():
            try:
                parsed = storage_mod.decode(body.decode("utf-8"))
            except ValueError, UnicodeDecodeError:
                return None
            if isinstance(parsed, dict):
                params.update(parsed)
            else:
                params["_json"] = parsed
    elif content_type == "application/x-www-form-urlencoded":
        body = (await request.body()).decode("utf-8", errors="replace")
        params.update(decode_pairs(parse_qsl(body, keep_blank_values=True)))
    elif content_type == "multipart/form-data":
        form = await request.form()
        params.update(decode_pairs((key, value) for key, value in form.multi_items() if isinstance(value, str)))
    return params


def create_app(storage_file_path: str | None = None) -> PathNormalizer:
    pubsub = PubSub()
    storage = Storage(storage_file_path or config.storage_file_path(), pubsub)
    scheduler = Scheduler(storage, pubsub)
    services = Services(storage=storage, scheduler=scheduler, pubsub=pubsub, templates=_templates())

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await asyncio.to_thread(scheduler.shutdown)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.services = services

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        status = 404 if exc.status_code == 405 else exc.status_code
        return negotiated_error(request, status)

    def render_page(request: Request, view_cls: type[LiveView]) -> Response:
        if _accepted_format(request.headers.get("accept"), ["html"]) is None:
            return negotiated_error(request, 406)
        set_route_attributes(LIVE_PLUG, "nil")
        view = view_cls(services)
        try:
            with live_span(f"{view.module}.mount"):
                view.mount()
        except Exception:
            logger.exception("Failed to mount %s", view.module)
            return error_response(500)
        token = request.cookies.get(SESSION_COOKIE) or secrets.token_urlsafe(32)
        response = Response(
            view.render_page(csrf_token=token),
            headers={"content-type": "text/html; charset=utf-8", **SECURE_BROWSER_HEADERS},
        )
        if request.cookies.get(SESSION_COOKIE) != token:
            response.set_cookie(SESSION_COOKIE, token, path="/", httponly=True, samesite="lax")
        return response

    @app.get("/")
    async def dashboard(request: Request) -> Response:
        return render_page(request, Dashboard)

    @app.get("/advanced")
    async def advanced(request: Request) -> Response:
        return render_page(request, AdvancedEditor)

    @app.get("/scheduler")
    async def scheduler_page(request: Request) -> Response:
        return render_page(request, SchedulerView)

    async def read(request: Request) -> Response:
        if _accepted_format(request.headers.get("accept"), ["json"]) is None:
            return negotiated_error(request, 406)
        set_route_attributes(FEATURE_CONTROLLER, "read")
        state = storage.read()
        if not isinstance(state, dict) or "flags" not in state:
            return error_response(500, "json")
        return json_response({"flags": state["flags"]})

    async def write(request: Request) -> Response:
        params = await _request_params(request)
        if params is None:
            return negotiated_error(request, 400)
        if _accepted_format(request.headers.get("accept"), ["json"]) is None:
            return negotiated_error(request, 406)
        set_route_attributes(FEATURE_CONTROLLER, "write")
        if "data" not in params:
            return error_response(400, "json")
        storage.replace(storage_mod.encode(params["data"]))
        return json_response({})

    app.add_api_route("/api/read", read, methods=["GET"])
    app.add_api_route("/api/read-file", read, methods=["GET"])
    app.add_api_route("/api/write", write, methods=["POST"])
    app.add_api_route("/api/write-to-file", write, methods=["POST"])

    @app.websocket("/live")
    async def live(websocket: WebSocket) -> None:
        await run_live_view(websocket, services)

    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> FileResponse:
        return FileResponse(STATIC_DIR / "favicon.ico")

    @app.get("/robots.txt", include_in_schema=False)
    async def robots() -> FileResponse:
        return FileResponse(STATIC_DIR / "robots.txt")

    FastAPIInstrumentor.instrument_app(
        app, excluded_urls="/live", exclude_spans=["receive", "send"]
    )

    wrapper = PathNormalizer(app)
    wrapper.services = services
    return wrapper


def _event_params(message: str) -> tuple[str | None, dict[str, Any]]:
    try:
        payload = json.loads(message)
    except ValueError:
        return None, {}
    if not isinstance(payload, dict):
        return None, {}
    payload.pop("HEADERS", None)
    event = payload.pop("__event", None)
    target = payload.get("__target")
    params = decode_pairs(flatten_values(payload))
    if target is not None:
        params["__target"] = target
        if target in payload:
            params[target] = payload[target]
    return (event if isinstance(event, str) else None), params


async def run_live_view(websocket: WebSocket, services: Services) -> None:
    view_cls = VIEWS.get(websocket.query_params.get("view", ""))
    token = websocket.query_params.get("_csrf_token")
    if view_cls is None or not token or token != websocket.cookies.get(SESSION_COOKIE):
        await websocket.close(code=1008)
        return
    await websocket.accept()

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    def deliver(message: Any) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, ("info", message))

    unsubscribers = [services.pubsub.subscribe(topic, deliver) for topic in view_cls.topics]
    tasks: list[asyncio.Task] = []
    try:
        view = view_cls(services)
        with live_span(f"{view.module}.mount"):
            view.mount()
        initial = view.on_connect()
        if initial:
            await websocket.send_text(initial)

        async def receive() -> None:
            try:
                while True:
                    queue.put_nowait(("event", await websocket.receive_text()))
            except WebSocketDisconnect, RuntimeError:
                queue.put_nowait(("closed", None))

        async def tick(interval_ms: int) -> None:
            while True:
                await asyncio.sleep(interval_ms / 1000)
                queue.put_nowait(("info", ("tick",)))

        tasks.append(asyncio.create_task(receive()))
        if view.tick_ms:
            tasks.append(asyncio.create_task(tick(view.tick_ms)))

        while True:
            kind, payload = await queue.get()
            if kind == "closed":
                break
            if kind == "info":
                html = view.handle_info(payload)
            else:
                event, params = _event_params(payload)
                if event is None:
                    continue
                if event == "lv:clear-flash":
                    view.clear_flash(params.get("key"))
                    html = view.render("partials/flashes.html")
                else:
                    with live_span(f"{view.module}.handle_event#{event}"):
                        html = view.handle_event(event, params)
            if html:
                await websocket.send_text(html)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("Live view %s crashed", view_cls.module)
        with contextlib.suppress(Exception):
            await websocket.close(code=1012)
    finally:
        for unsubscribe in unsubscribers:
            unsubscribe()
        for task in tasks:
            task.cancel()
