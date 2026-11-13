#!/usr/bin/python
# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import time

import pytest

from flagd_ui.scheduler import ALL, schedulable_flags

from .conftest import eventually, variant_of


# Every activation is held for the whole interval, so the offset within the
# interval is always zero and only the flag and variant picks consume randomness.
def immediate_config(**overrides):
    return {
        "interval_ms": 1000,
        "min_duration_ms": 1000,
        "max_duration_ms": 1000,
        "seed": 42,
        "flags": ALL,
        **overrides,
    }


def held_config(**overrides):
    return immediate_config(
        **{"interval_ms": 60_000, "min_duration_ms": 60_000, "max_duration_ms": 60_000, **overrides}
    )


def active_with(count):
    return lambda m: m[0] == "scheduler_state" and len(m[1]["active"]) == count


class TestSchedulableFlags:
    def test_offers_every_failure_flag_with_its_non_off_variants(self, storage):
        flags = schedulable_flags(storage.read())

        assert ("adFailure", ["on"]) in flags
        assert ("imageSlowLoad", ["10sec", "5sec"]) in flags
        assert ("cartFailure", ["10%", "100%", "25%", "50%", "75%", "90%"]) in flags

    def test_excludes_emit_raw_pii_which_is_marked_non_schedulable(self, storage):
        assert all(name != "emitRawPii" for name, _ in schedulable_flags(storage.read()))

    def test_tolerates_a_configuration_without_flags(self):
        assert schedulable_flags({}) == []
        assert schedulable_flags({"flags": None}) == []


class TestValidation:
    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            ({"interval_ms": 500}, "Interval must be at least 1 second"),
            ({"min_duration_ms": 5000, "max_duration_ms": 2000}, "greater than or equal to the minimum"),
            ({"interval_ms": 2000, "max_duration_ms": 5000}, "must fit within the interval"),
            ({"flags": {}}, "at least one flag variant"),
            ({"flags": {"adFailure": [], "cartFailure": []}}, "at least one flag variant"),
            ({"concurrency": 0}, "At least one flag has to be activated"),
        ],
    )
    def test_rejects_unusable_configurations(self, make_scheduler, overrides, message):
        error = make_scheduler().start_schedule(immediate_config(**overrides))
        assert error is not None and message in error

    def test_leaves_the_scheduler_stopped_after_a_rejected_configuration(self, make_scheduler):
        scheduler = make_scheduler()
        scheduler.start_schedule(immediate_config(interval_ms=500))
        assert not scheduler.state()["running"]


class TestRunning:
    def test_activates_a_flag_and_reports_it_as_active(self, make_scheduler, storage, inbox):
        messages = inbox("scheduler")
        scheduler = make_scheduler()

        assert scheduler.start_schedule(immediate_config()) is None

        active = messages.receive(active_with(1))[1]["active"][0]
        assert active["flag"] in [name for name, _ in schedulable_flags(storage.read())]
        assert active["variant"] != "off"
        assert eventually(lambda: variant_of(storage, active["flag"]) == active["variant"])

    def test_turns_the_flag_back_off_when_the_hold_expires(self, make_scheduler, storage, inbox):
        messages = inbox("scheduler")
        scheduler = make_scheduler()

        config = immediate_config(interval_ms=5000, min_duration_ms=1000, max_duration_ms=1000)
        assert scheduler.start_schedule(config) is None

        flag = messages.receive(active_with(1), timeout=5)[1]["active"][0]["flag"]

        assert eventually(lambda: variant_of(storage, flag) == "off", 2)
        assert any(entry["flag"] == flag for entry in scheduler.state()["history"])

    def test_reverts_every_flag_it_activates_when_holds_fill_the_whole_interval(
        self, make_scheduler, storage, inbox
    ):
        messages = inbox("scheduler")
        scheduler = make_scheduler()

        assert scheduler.start_schedule(immediate_config(seed=99)) is None
        messages.receive(active_with(1))

        time.sleep(3.5)
        scheduler.stop_schedule()

        state = scheduler.state()
        touched = {entry["flag"] for entry in state["history"]}

        assert len(touched) > 1
        assert state["active"] == []
        for flag in touched:
            assert eventually(lambda flag=flag: variant_of(storage, flag) == "off")

    def test_stopping_reverts_a_flag_that_is_still_active(self, make_scheduler, storage, inbox):
        messages = inbox("scheduler")
        scheduler = make_scheduler()

        assert scheduler.start_schedule(held_config()) is None
        flag = messages.receive(active_with(1))[1]["active"][0]["flag"]

        scheduler.stop_schedule()

        state = scheduler.state()
        assert not state["running"]
        assert state["active"] == []
        assert eventually(lambda: variant_of(storage, flag) == "off")

    def test_restricts_activations_to_the_selected_flags(self, make_scheduler, inbox):
        messages = inbox("scheduler")
        scheduler = make_scheduler()

        assert scheduler.start_schedule(immediate_config(flags={"adHighCpu": ["on"]})) is None

        active = messages.receive(active_with(1))[1]["active"][0]
        assert (active["flag"], active["variant"]) == ("adHighCpu", "on")

    def test_restricts_activations_to_the_selected_variants_of_a_flag(self, make_scheduler, inbox):
        messages = inbox("scheduler")
        scheduler = make_scheduler()

        assert scheduler.start_schedule(immediate_config(flags={"cartFailure": ["10%", "25%"]})) is None

        seen = 0
        last = None
        while seen < 4:
            active = messages.receive(active_with(1))[1]["active"][0]
            assert active["flag"] == "cartFailure"
            assert active["variant"] in ["10%", "25%"]
            if active["started_at"] != last:
                last = active["started_at"]
                seen += 1

    def test_ignores_selected_variants_a_flag_no_longer_offers(self, make_scheduler, inbox):
        messages = inbox("scheduler")
        scheduler = make_scheduler()

        config = immediate_config(flags={"adFailure": ["on"], "cartFailure": ["200%"]})
        assert scheduler.start_schedule(config) is None

        active = messages.receive(active_with(1))[1]["active"][0]
        assert (active["flag"], active["variant"]) == ("adFailure", "on")

    def test_the_same_seed_picks_the_same_flag_and_variant(self, make_scheduler, inbox):
        messages = inbox("scheduler")

        first = make_scheduler()
        assert first.start_schedule(immediate_config(seed=1234)) is None
        picked = messages.receive(active_with(1))[1]["active"][0]
        first.stop_schedule()

        second = make_scheduler()
        assert second.start_schedule(immediate_config(seed=1234)) is None
        again = messages.receive(active_with(1))[1]["active"][0]
        second.stop_schedule()

        assert (again["flag"], again["variant"]) == (picked["flag"], picked["variant"])

    def test_holds_several_distinct_flags_active_at_once(self, make_scheduler, storage, inbox):
        messages = inbox("scheduler")
        scheduler = make_scheduler()

        assert scheduler.start_schedule(held_config(concurrency=3)) is None

        flags = [a["flag"] for a in messages.receive(active_with(3))[1]["active"]]
        assert len(set(flags)) == 3
        for flag in flags:
            assert eventually(lambda flag=flag: variant_of(storage, flag) != "off")

    def test_never_activates_the_same_flag_twice_within_an_interval(self, make_scheduler):
        scheduler = make_scheduler()

        assert scheduler.start_schedule(held_config(concurrency=5)) is None

        state = scheduler.state()
        flags = [p["flag"] for p in state["next_up"]] + [a["flag"] for a in state["active"]]
        assert len(flags) == 5
        assert len(set(flags)) == 5

    def test_settles_for_the_flags_that_exist_when_asked_for_more(self, make_scheduler, inbox):
        messages = inbox("scheduler")
        scheduler = make_scheduler()

        config = held_config(concurrency=99, flags={"adFailure": ["on"]})
        assert scheduler.start_schedule(config) is None

        assert messages.receive(active_with(1))[1]["active"][0]["flag"] == "adFailure"

    def test_reverts_every_concurrently_held_flag_when_stopped(self, make_scheduler, storage, inbox):
        messages = inbox("scheduler")
        scheduler = make_scheduler()

        assert scheduler.start_schedule(held_config(concurrency=4)) is None
        flags = [a["flag"] for a in messages.receive(active_with(4))[1]["active"]]

        scheduler.stop_schedule()

        assert scheduler.state()["active"] == []
        for flag in flags:
            assert eventually(lambda flag=flag: variant_of(storage, flag) == "off")

    def test_reports_the_seed_it_used_when_none_was_given(self, make_scheduler):
        scheduler = make_scheduler()

        assert scheduler.start_schedule(immediate_config(seed=None)) is None
        assert isinstance(scheduler.state()["seed_used"], int)

    def test_shutdown_reverts_held_flags(self, make_scheduler, storage, inbox):
        messages = inbox("scheduler")
        scheduler = make_scheduler()

        assert scheduler.start_schedule(held_config()) is None
        flag = messages.receive(active_with(1))[1]["active"][0]["flag"]

        scheduler.shutdown()

        assert variant_of(storage, flag) == "off"
