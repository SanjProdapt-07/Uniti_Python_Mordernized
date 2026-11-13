#!/usr/bin/python
# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import json
import re

import pytest
from markupsafe import escape
from starlette.websockets import WebSocketDisconnect

from flagd_ui.web import SESSION_COOKIE

from .conftest import LiveSession, eventually, variant_of


def hrefs(html: str) -> dict[str, str]:
    return {label: href for href, label in re.findall(r'<a href="([^"]+)"[^>]*>\s*(\w+)\s*</a>', html)}


@pytest.mark.parametrize("path", ["/", "/feature", "/feature/", "/advanced", "/feature/scheduler"])
def test_pages_render_the_navbar(client, path):
    response = client.get(path)

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    assert "Flagd Configurator" in response.text


def test_navbar_links_use_the_feature_base_path_and_resolve(client):
    links = hrefs(client.get("/").text)

    assert links == {"Basic": "/feature", "Advanced": "/feature/advanced", "Scheduler": "/feature/scheduler"}
    for href in links.values():
        assert "Flagd Configurator" in client.get(href).text


def test_navbar_highlights_the_current_page(client):
    html = client.get("/feature/advanced").text
    active = re.search(r'<a href="/feature/advanced" class="([^"]+)"', html).group(1)
    assert "bg-blue-700" in active


def test_static_assets_are_served(client):
    html = client.get("/").text
    css = re.search(r'href="(/feature/assets/css/app\.css\?vsn=\w+)"', html).group(1)
    assert client.get(css).status_code == 200
    for asset in ["/feature/assets/js/app.js", "/feature/assets/vendor/htmx.min.js", "/favicon.ico", "/robots.txt"]:
        assert client.get(asset).status_code == 200


def test_dashboard_shows_every_flag_with_its_variants(client):
    html = client.get("/").text

    for name in ["adFailure", "cartFailure", "productCatalogFailure"]:
        assert f'name="{name}"' in html
    assert "Fail ad service" in html
    assert '<option value="10%">10%</option>' in html
    assert '<option value="off" selected>off</option>' in html
    assert html.count("This flag has targeting rules.") == 1


def test_pages_set_the_session_cookie(client):
    client.get("/")
    assert client.cookies.get(SESSION_COOKIE)


def test_live_socket_requires_the_csrf_token(client):
    client.get("/")
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect("/feature/live?view=dashboard&_csrf_token=wrong") as socket:
            socket.receive_text()
    assert closed.value.code == 1008


def test_dashboard_toggle_writes_the_flag_and_flashes(live, app, flag_file):
    storage = app.services.storage
    with live("/", "dashboard") as socket:
        session = LiveSession(socket)
        session.receive_until('id="flags-grid"')

        session.push("flag_changed", __target="adFailure", adFailure="on", cartFailure="off")

        assert "Saved: adFailure" in session.receive_until("flash-slot-info")
        card = session.receive_until('<option value="on" selected>on</option>')
        assert "flag-" + b"adFailure".hex() in card

    assert variant_of(storage, "adFailure") == "on"
    assert json.loads(flag_file.read_text())["flags"]["adFailure"]["defaultVariant"] == "on"


def test_dashboard_toggle_of_a_targeted_flag_flips_the_rule(live, app):
    with live("/", "dashboard") as socket:
        session = LiveSession(socket)
        session.receive_until('id="flags-grid"')
        session.push("flag_changed", __target="productCatalogFailure", productCatalogFailure="on")
        session.receive_until("Saved: productCatalogFailure")

    flag = app.services.storage.read()["flags"]["productCatalogFailure"]
    assert flag["targeting"]["if"][1] == "on"


def test_dashboard_receives_changes_made_elsewhere(live, client):
    with live("/", "dashboard") as socket:
        session = LiveSession(socket)
        session.receive_until('id="flags-grid"')

        flags = client.get("/api/read").json()["flags"]
        flags["adHighCpu"]["defaultVariant"] = "on"
        client.post("/api/write", json={"data": {"flags": flags}})

        card = session.receive_until("flag-" + b"adHighCpu".hex())
        assert '<option value="on" selected>on</option>' in card


def test_clicking_a_flash_clears_it(live):
    with live("/", "dashboard") as socket:
        session = LiveSession(socket)
        session.receive_until('id="flags-grid"')
        session.push("flag_changed", __target="adFailure", adFailure="on")
        session.receive_until("Saved: adFailure")

        session.push("lv:clear-flash", key="info")
        html = session.receive_until('<div id="flash-slot-info"></div>')
        assert "Saved" not in html


class TestAdvancedEditor:
    def test_shows_the_configuration_as_sorted_pretty_json(self, client, app):
        html = client.get("/advanced").text
        expected = json.dumps(app.services.storage.read(), indent=2, sort_keys=True, ensure_ascii=False)
        assert str(escape(expected)) in html
        assert "Unsaved changes" not in html

    def test_editing_marks_unsaved_changes(self, live):
        with live("/advanced", "advanced") as socket:
            session = LiveSession(socket)
            session.push("edit", content="{}")
            assert "Unsaved changes" in session.receive_until("unsaved-slot")

    def test_invalid_json_is_rejected(self, live, flag_file):
        before = flag_file.read_text()
        with live("/advanced", "advanced") as socket:
            session = LiveSession(socket)
            session.push("edit", content="{nope")
            session.receive_until("Unsaved changes")
            session.push("save", content="{nope")
            assert "Invalid JSON" in session.receive_until("flash-slot-error")
        assert flag_file.read_text() == before

    def test_valid_json_is_saved_trimmed(self, live, flag_file, app):
        content = '\n  {"flags": {"adFailure": {"defaultVariant": "on", "variants": {"on": true, "off": false}}}}  \n'
        with live("/advanced", "advanced") as socket:
            session = LiveSession(socket)
            session.push("save", content=content)
            html = session.receive_until("Saved!")
            assert '<div id="unsaved-slot"></div>' in html
            assert 'id="editor"' in html
        assert flag_file.read_text() == content.strip()
        assert app.services.storage.read()["flags"]["adFailure"]["defaultVariant"] == "on"


class TestSchedulerPage:
    @pytest.fixture(autouse=True)
    def stop_scheduler(self, app):
        yield
        app.services.scheduler.stop_schedule()

    def form(self, **overrides):
        return {
            "interval_seconds": "600",
            "min_duration_seconds": "600",
            "max_duration_seconds": "600",
            "concurrency": "1",
            "seed": "",
            "variants[adFailure][]": "on",
            **overrides,
        }

    def test_renders_the_schedulable_flags_and_starts_out_idle(self, client):
        html = client.get("/scheduler").text

        assert "Scheduler stopped" in html
        assert "Idle" in html
        assert "adFailure" in html
        assert 'value="900"' in html

    def test_offers_a_checkbox_per_variant(self, client):
        html = client.get("/scheduler").text

        for variant in ["10%", "25%", "50%", "75%", "90%", "100%"]:
            assert f'name="variants[cartFailure][]" value="{variant}" checked' in html
        assert 'name="variants[adFailure][]" value="on"' in html
        assert "emitRawPii" not in html

    def test_surfaces_a_validation_error(self, live, app):
        with live("/scheduler", "scheduler") as socket:
            session = LiveSession(socket)
            session.push("start", **self.form(interval_seconds="10", min_duration_seconds="20", max_duration_seconds="30"))
            assert "must fit within the interval" in session.receive_until("scheduler-error")
        assert not app.services.scheduler.state()["running"]

    def test_rejects_a_non_numeric_interval(self, live):
        with live("/scheduler", "scheduler") as socket:
            session = LiveSession(socket)
            session.push("start", **self.form(interval_seconds="soon"))
            assert "whole number of seconds" in session.receive_until("scheduler-error")

    def test_starting_reports_it_as_running(self, live, app):
        with live("/scheduler", "scheduler") as socket:
            session = LiveSession(socket)
            session.push("start", **self.form(seed="7"))
            html = session.receive_until("Scheduler started", "Scheduler running")
            assert "Active" in html
            assert "Restart" in html
            assert ">\n    Stop\n" in html

        state = app.services.scheduler.state()
        assert state["running"]
        assert state["seed_used"] == 7

    def test_stopping_reports_it_as_stopped(self, live, app):
        with live("/scheduler", "scheduler") as socket:
            session = LiveSession(socket)
            session.push("start", **self.form())
            session.receive_until("Scheduler running")
            session.push("stop")
            html = session.receive_until("Scheduler stopped", "flash-info")
            assert "Scheduler stopped" in html
        assert not app.services.scheduler.state()["running"]

    def test_a_preset_fills_in_the_timing_fields(self, live):
        with live("/scheduler", "scheduler") as socket:
            session = LiveSession(socket)
            session.push("preset", name="presentation", **self.form())
            html = session.receive_until("scheduler-timing")
            for value in ["60", "10", "20"]:
                assert f'value="{value}"' in html

    def test_clearing_the_flag_selection_is_rejected(self, live):
        with live("/scheduler", "scheduler") as socket:
            session = LiveSession(socket)
            session.push("select_none")
            html = session.receive_until("scheduler-flags")
            assert " checked" not in html
            form = {k: v for k, v in self.form().items() if not k.startswith("variants")}
            session.push("start", **form)
            assert "at least one flag variant" in session.receive_until("scheduler-error")

    def test_scheduling_only_the_gentle_percentages(self, live, app):
        with live("/scheduler", "scheduler") as socket:
            session = LiveSession(socket)
            form = {k: v for k, v in self.form(seed="3").items() if not k.startswith("variants")}
            session.push("start", **form, **{"variants[cartFailure][]": ["10%", "25%"]})
            session.receive_until("Scheduler running")

        assert app.services.scheduler.state()["config"]["flags"] == {"cartFailure": ["10%", "25%"]}

    def test_toggling_a_flag_clears_and_restores_its_variants(self, live):
        all_cart = {"variants[cartFailure][]": ["10%", "100%", "25%", "50%", "75%", "90%"]}
        with live("/scheduler", "scheduler") as socket:
            session = LiveSession(socket)
            session.push("toggle_flag", flag="cartFailure", **all_cart)
            html = session.receive_until("scheduler-flags")
            assert 'name="variants[cartFailure][]" value="10%" checked' not in html

            session.push("toggle_flag", flag="cartFailure")
            html = session.receive_until("scheduler-flags")
            assert 'name="variants[cartFailure][]" value="10%" checked' in html

    def test_select_all_checks_every_variant(self, live):
        with live("/scheduler", "scheduler") as socket:
            session = LiveSession(socket)
            session.push("select_all")
            html = session.receive_until("scheduler-flags")
            assert 'name="variants[aiSlowResponse][]" value="5sec" checked' in html

    def test_running_scheduler_shows_the_active_flag_and_history(self, live, app):
        with live("/scheduler", "scheduler") as socket:
            session = LiveSession(socket)
            session.push(
                "start",
                **self.form(interval_seconds="2", min_duration_seconds="1", max_duration_seconds="1", seed="5"),
            )
            assert "adFailure = on" in session.receive_until("Currently active (1 of 1)", "adFailure = on")
            history = session.receive_until("Recent activations", limit=40)
            assert re.search(r"\d\d:\d\d:\d\d UTC for 1s", history)

        assert eventually(lambda: app.services.scheduler.state()["history"] != [])
