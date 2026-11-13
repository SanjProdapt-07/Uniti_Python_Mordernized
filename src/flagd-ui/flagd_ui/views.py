#!/usr/bin/python
# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import re
from dataclasses import dataclass
from typing import Any

from jinja2 import Environment

from . import scheduler as scheduler_mod
from . import storage as storage_mod
from .pubsub import PubSub
from .scheduler import Scheduler, monotonic_ms
from .storage import Storage


@dataclass
class Services:
    storage: Storage
    scheduler: Scheduler
    pubsub: PubSub
    templates: Environment


class LiveView:
    module = ""
    view_name = ""
    template = ""
    mode = "basic"
    topics: tuple[str, ...] = ()
    tick_ms: int | None = None

    def __init__(self, services: Services) -> None:
        self.services = services
        self.flash: dict[str, str] = {}
        self.assigns: dict[str, Any] = {}

    def mount(self) -> None:
        raise NotImplementedError

    def context(self) -> dict[str, Any]:
        return {**self.assigns, "flash": self.flash, "mode": self.mode, "view_name": self.view_name}

    def render(self, *templates: str, **extra: Any) -> str:
        context = {**self.context(), **extra}
        return "".join(self.services.templates.get_template(t).render(context) for t in templates)

    def render_page(self, **extra: Any) -> str:
        return self.render(self.template, **extra)

    def on_connect(self) -> str:
        return ""

    def put_flash(self, kind: str, message: str) -> None:
        self.flash[kind] = message

    def clear_flash(self, key: str | None = None) -> None:
        if key is None:
            self.flash = {}
        else:
            self.flash.pop(key, None)

    def handle_event(self, event: str, params: dict[str, Any]) -> str:
        return ""

    def handle_info(self, message: tuple) -> str:
        return ""


def variants_of(data: Any) -> list[str]:
    variants = data.get("variants") if isinstance(data, dict) else None
    return sorted(variants) if isinstance(variants, dict) else []


def selected_variant(data: Any) -> Any:
    if not isinstance(data, dict):
        return None
    targeting = data.get("targeting")
    rule = targeting.get("if") if isinstance(targeting, dict) else None
    if isinstance(rule, list) and len(rule) >= 2:
        return rule[1]
    return data.get("defaultVariant")


def has_targeting(data: Any) -> bool:
    return isinstance(data, dict) and "targeting" in data


def field(data: Any, key: str) -> Any:
    value = data.get(key) if isinstance(data, dict) else None
    return "" if value is None else value


def flag_dom_id(name: str) -> str:
    return "flag-" + name.encode().hex()


class Dashboard(LiveView):
    module = "FlagdUiWeb.Dashboard"
    view_name = "dashboard"
    template = "dashboard.html"
    topics = (storage_mod.TOPIC,)

    def mount(self) -> None:
        state = self.services.storage.read()
        if not isinstance(state, dict) or "flags" not in state:
            raise LookupError("flagd configuration has no flags")
        self.assigns["flags"] = _sorted_flags(state["flags"])

    def on_connect(self) -> str:
        return self.render("partials/dashboard_grid.html")

    def handle_event(self, event: str, params: dict[str, Any]) -> str:
        if event != "flag_changed":
            return ""
        target = params.get("__target")
        variant = params.get(target)
        self.services.storage.write(target, variant)
        self.put_flash("info", f"Saved: {target}")
        return self.render("partials/flashes.html")

    def handle_info(self, message: tuple) -> str:
        if message[0] != "flags_changed":
            return ""
        state = message[1]
        if not isinstance(state, dict) or "flags" not in state:
            return ""
        old = self.assigns.get("flags", {})
        new = _sorted_flags(state["flags"])
        self.assigns["flags"] = new
        if list(old) != list(new):
            return self.render("partials/dashboard_grid.html")
        return "".join(
            self.render("partials/dashboard_card.html", name=name, data=data)
            for name, data in new.items()
            if old.get(name) != data
        )


def _sorted_flags(flags: Any) -> dict[str, Any]:
    if not isinstance(flags, dict):
        return {}
    return {name: flags[name] for name in sorted(flags)}


class AdvancedEditor(LiveView):
    module = "FlagdUiWeb.AdvancedEditor"
    view_name = "advanced"
    template = "advanced.html"
    mode = "advanced"

    def mount(self) -> None:
        state = self.services.storage.read()
        self.assigns["content"] = storage_mod.encode(state, pretty=True)
        self.assigns["unsaved_changes"] = False

    def handle_event(self, event: str, params: dict[str, Any]) -> str:
        if event == "edit":
            self.assigns["content"] = params.get("content", "")
            self.assigns["unsaved_changes"] = True
            return self.render("partials/unsaved.html")
        if event == "save":
            content = params.get("content", self.assigns["content"])
            if not isinstance(content, str):
                content = ""
            self.assigns["content"] = content
            try:
                storage_mod.decode(content)
            except ValueError:
                self.put_flash("error", "Invalid JSON")
                return self.render("partials/flashes.html")
            trimmed = content.strip()
            self.services.storage.replace(trimmed)
            self.assigns["unsaved_changes"] = False
            self.assigns["content"] = trimmed
            self.clear_flash()
            self.put_flash("info", "Saved!")
            return self.render("partials/flashes.html", "partials/unsaved.html", "partials/editor.html")
        return ""


PRESETS = {
    "presentation": {"interval_seconds": 60, "min_duration_seconds": 10, "max_duration_seconds": 20},
    "hourly": {"interval_seconds": 3600, "min_duration_seconds": 300, "max_duration_seconds": 900},
    "frequent": {"interval_seconds": 900, "min_duration_seconds": 60, "max_duration_seconds": 180},
}

PRESET_LABELS = [
    ("presentation", "Presentation (60s / 10-20s)"),
    ("frequent", "Frequent (15m / 1-3m)"),
    ("hourly", "Hourly (1h / 5-15m)"),
]

FORM_FIELDS = ("interval_seconds", "min_duration_seconds", "max_duration_seconds", "concurrency", "seed")

# Matches an optionally signed run of decimal digits, the whole of Integer.parse/1 input.
_INTEGER = re.compile(r"[+-]?\d+")


def format_ms(ms: int | float) -> str:
    if ms <= 0:
        return "0s"
    seconds = int(ms) // 1000
    minutes = seconds // 60
    hours = minutes // 60
    if hours > 0:
        return f"{hours}h {minutes % 60}m"
    if minutes > 0:
        return f"{minutes}m {seconds % 60}s"
    return f"{seconds}s"


def next_trigger_label(scheduler: dict[str, Any], now: int) -> str:
    at = scheduler.get("next_trigger_at")
    return "unknown" if at is None else format_ms(at - now)


def is_selected(form: dict[str, Any], flag: str, variant: str) -> bool:
    chosen = selection(form).get(flag) or []
    return isinstance(chosen, list) and variant in chosen


def selection(form: dict[str, Any]) -> dict[str, Any]:
    chosen = form.get("variants")
    return chosen if isinstance(chosen, dict) else {}


def merge_params(form: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    merged = {**form, **{key: params[key] for key in FORM_FIELDS if key in params}}
    variants = params.get("variants", {})
    merged["variants"] = {
        name: list(chosen) if isinstance(chosen, list) else [chosen]
        for name, chosen in (variants.items() if isinstance(variants, dict) else [])
    }
    return merged


def _parse_positive_integer(value: Any, message: str) -> tuple[int | None, str | None]:
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if isinstance(value, str):
        trimmed = value.strip()
        if _INTEGER.fullmatch(trimmed) and int(trimmed) > 0:
            return int(trimmed), None
    return None, message


def _parse_seed(value: Any) -> tuple[int | None, str | None]:
    if value is None:
        return None, None
    if isinstance(value, int) and not isinstance(value, bool):
        return value, None
    if isinstance(value, str):
        trimmed = value.strip()
        if trimmed == "":
            return None, None
        if _INTEGER.fullmatch(trimmed):
            return int(trimmed), None
    return None, "Seed must be a whole number"


def build_config(form: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    values = {}
    for key, label in (
        ("interval_seconds", "Interval"),
        ("min_duration_seconds", "Minimum duration"),
        ("max_duration_seconds", "Maximum duration"),
    ):
        values[key], error = _parse_positive_integer(
            form.get(key), f"{label} must be a whole number of seconds greater than zero"
        )
        if error:
            return None, error
    concurrency, error = _parse_positive_integer(
        form.get("concurrency"), "Flags at once must be a whole number greater than zero"
    )
    if error:
        return None, error
    seed, error = _parse_seed(form.get("seed"))
    if error:
        return None, error
    return {
        "interval_ms": values["interval_seconds"] * 1000,
        "min_duration_ms": values["min_duration_seconds"] * 1000,
        "max_duration_ms": values["max_duration_seconds"] * 1000,
        "concurrency": concurrency,
        "seed": seed,
        "flags": selection(form),
    }, None


class SchedulerView(LiveView):
    module = "FlagdUiWeb.Scheduler"
    view_name = "scheduler"
    template = "scheduler.html"
    mode = "scheduler"
    topics = (scheduler_mod.TOPIC, storage_mod.TOPIC)
    tick_ms = 1000

    def mount(self) -> None:
        state = self.services.scheduler.state()
        available = self._available_flags()
        self.assigns.update(
            scheduler=state,
            available=available,
            form=self._form_from(state, available),
            now=monotonic_ms(),
            error=None,
        )

    def context(self) -> dict[str, Any]:
        return {**super().context(), "preset_labels": PRESET_LABELS}

    def on_connect(self) -> str:
        return self._render_state()

    def _available_flags(self) -> list[tuple[str, list[str]]]:
        return scheduler_mod.schedulable_flags(self.services.storage.read())

    @staticmethod
    def _form_from(state: dict[str, Any], available: list[tuple[str, list[str]]]) -> dict[str, Any]:
        config = state["config"]
        flags = config["flags"]
        return {
            "interval_seconds": config["interval_ms"] // 1000,
            "min_duration_seconds": config["min_duration_ms"] // 1000,
            "max_duration_seconds": config["max_duration_ms"] // 1000,
            "concurrency": config["concurrency"],
            "seed": config["seed"],
            "variants": dict(available) if flags == scheduler_mod.ALL else flags,
        }

    def _render_state(self) -> str:
        return self.render(
            "partials/scheduler_status.html",
            "partials/scheduler_actions.html",
            "partials/scheduler_history.html",
        )

    def _put_selection(self, chosen: dict[str, Any]) -> None:
        self.assigns["form"] = {**self.assigns["form"], "variants": chosen}

    def handle_event(self, event: str, params: dict[str, Any]) -> str:
        form = self.assigns["form"]
        if event in ("validate", "preset", "select_all", "select_none", "toggle_flag", "start"):
            form = self.assigns["form"] = merge_params(form, params)

        if event == "validate":
            return ""
        if event == "preset":
            preset = PRESETS[params["name"]]
            self.assigns["form"] = {**form, **preset}
            return self.render("partials/scheduler_timing.html")
        if event == "select_all":
            self._put_selection(dict(self.assigns["available"]))
            return self.render("partials/scheduler_flags.html")
        if event == "select_none":
            self._put_selection({})
            return self.render("partials/scheduler_flags.html")
        if event == "toggle_flag":
            flag = params.get("flag")
            variants = next((v for name, v in self.assigns["available"] if name == flag), [])
            chosen = [] if selection(form).get(flag) else variants
            self._put_selection({**selection(form), flag: chosen})
            return self.render("partials/scheduler_flags.html")
        if event == "start":
            config, error = build_config(form)
            if error is None:
                error = self.services.scheduler.start_schedule(config)
            if error is not None:
                self.assigns["error"] = error
                return self.render("partials/scheduler_error.html")
            self.assigns.update(error=None, scheduler=self.services.scheduler.state())
            self.clear_flash()
            self.put_flash("info", "Scheduler started")
            return self.render("partials/scheduler_error.html", "partials/flashes.html") + self._render_state()
        if event == "stop":
            self.services.scheduler.stop_schedule()
            self.assigns.update(error=None, scheduler=self.services.scheduler.state())
            self.clear_flash()
            self.put_flash("info", "Scheduler stopped")
            return self.render("partials/scheduler_error.html", "partials/flashes.html") + self._render_state()
        return ""

    def handle_info(self, message: tuple) -> str:
        kind = message[0]
        if kind == "scheduler_state":
            self.assigns["scheduler"] = message[1]
            return self._render_state()
        if kind == "flags_changed":
            before = len(self.assigns["available"])
            self.assigns["available"] = self._available_flags()
            html = self.render("partials/scheduler_flags.html")
            if len(self.assigns["available"]) != before:
                html += self.render("partials/scheduler_timing.html")
            return html
        if kind == "tick":
            self.assigns["now"] = monotonic_ms()
            return self.render("partials/scheduler_status.html")
        return ""


VIEWS: dict[str, type[LiveView]] = {
    view.view_name: view for view in (Dashboard, AdvancedEditor, SchedulerView)
}
