#!/usr/bin/python
# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

"""Periodically activates randomly picked feature flags for a random amount of
time, so that failure scenarios appear and disappear on their own.

Each interval activates up to ``concurrency`` distinct flags. Every activation
gets its own hold duration between the configured minimum and maximum, and its
own offset, so that the whole activation fits inside the interval. A flag is
picked first and one of its selected variants second.
"""

import heapq
import itertools
import logging
import random
import secrets
import threading
import time
from datetime import UTC, datetime
from typing import Any

from .pubsub import PubSub
from .storage import Storage

logger = logging.getLogger(__name__)

TOPIC = "scheduler"
ALL = "all"
HISTORY_LIMIT = 10

DEFAULT_CONFIG: dict[str, Any] = {
    "interval_ms": 15 * 60 * 1000,
    "min_duration_ms": 60 * 1000,
    "max_duration_ms": 3 * 60 * 1000,
    "concurrency": 1,
    "seed": None,
    "flags": ALL,
}


def monotonic_ms() -> int:
    return int(time.monotonic() * 1000)


def _variant_names(data: Any) -> list[str]:
    variants = data.get("variants") if isinstance(data, dict) else None
    return list(variants.keys()) if isinstance(variants, dict) else []


def _flag_spec(name: str, data: Any) -> list[tuple[str, Any, list[str]]]:
    variants = _variant_names(data)
    resting = data.get("defaultVariant") if isinstance(data, dict) else None
    activatable = sorted(v for v in variants if v != resting)
    metadata = data.get("metadata") if isinstance(data, dict) else None
    schedulable = not (isinstance(metadata, dict) and metadata.get("schedulable") is False)
    if schedulable and resting in variants and activatable:
        return [(name, resting, activatable)]
    return []


def _eligible_flag_specs(config: Any) -> list[tuple[str, Any, list[str]]]:
    flags = config.get("flags") if isinstance(config, dict) else None
    if not isinstance(flags, dict):
        return []
    specs = [spec for name, data in flags.items() for spec in _flag_spec(name, data)]
    return sorted(specs, key=lambda spec: spec[0])


def schedulable_flags(config: Any) -> list[tuple[str, list[str]]]:
    """Flags eligible for scheduling as ``(name, activatable_variants)`` pairs.

    A flag is eligible when it has a resting variant (its configured default)
    to return to, at least one other variant, and is not opted out through
    ``metadata.schedulable: false``.
    """
    return [(name, activatable) for name, _resting, activatable in _eligible_flag_specs(config)]


def _validate(config: dict[str, Any]) -> str | None:
    if config["interval_ms"] < 1000:
        return "Interval must be at least 1 second"
    if config["min_duration_ms"] < 1000:
        return "Minimum duration must be at least 1 second"
    if config["max_duration_ms"] < config["min_duration_ms"]:
        return "Maximum duration must be greater than or equal to the minimum duration"
    if config["max_duration_ms"] > config["interval_ms"]:
        return "Maximum duration must fit within the interval"
    if config["concurrency"] < 1:
        return "At least one flag has to be activated per interval"
    if config["flags"] != ALL and _selected_variant_count(config["flags"]) == 0:
        return "Select at least one flag variant to schedule"
    return None


def _selected_variant_count(selection: Any) -> int:
    if not isinstance(selection, dict):
        return 0
    return sum(len(variants) for variants in selection.values())


class Scheduler:
    def __init__(self, storage: Storage, pubsub: PubSub) -> None:
        self._storage = storage
        self._pubsub = pubsub
        self._cond = threading.Condition(threading.RLock())
        self._queue: list[tuple[float, int, tuple]] = []
        self._seq = itertools.count()
        self._live: set[int] = set()
        self._stopped = False

        self._config: dict[str, Any] = dict(DEFAULT_CONFIG)
        self._running = False
        self._epoch = 0
        self._rand: random.Random | None = None
        self._seed_used: int | None = None
        self._interval_count = 0
        self._next_up: list[dict[str, Any]] = []
        self._active: dict[str, dict[str, Any]] = {}
        self._history: list[dict[str, Any]] = []
        self._timers: dict[Any, int] = {}
        self._eligible: list[tuple[str, Any, list[str]]] = []

        self._thread = threading.Thread(target=self._loop, name="flag-scheduler", daemon=True)
        self._thread.start()

    def state(self) -> dict[str, Any]:
        with self._cond:
            return self._public_state()

    def start_schedule(self, config: dict[str, Any]) -> str | None:
        """Starts scheduling, replacing any run in progress. Returns an error message
        when the configuration is not usable, ``None`` otherwise."""
        with self._cond:
            config = {**DEFAULT_CONFIG, **config}
            error = _validate(config)
            if error is not None:
                return error

            seed = config["seed"]
            if seed is None:
                seed = secrets.randbelow(2**31 - 1) + 1
            self._cancel_timers()
            self._revert_all()

            self._eligible = _eligible_flag_specs(self._storage.read())
            self._config = config
            self._running = True
            self._epoch += 1
            self._rand = random.Random(seed)
            self._seed_used = seed
            self._interval_count = 0
            self._history = []
            self._begin_interval()

            logger.info("Flag scheduler started with seed %s", seed)
            self._broadcast()
            return None

    def stop_schedule(self) -> None:
        with self._cond:
            self._cancel_timers()
            self._revert_all()
            self._running = False
            self._epoch += 1
            self._next_up = []
            logger.info("Flag scheduler stopped")
            self._broadcast()

    def shutdown(self) -> None:
        with self._cond:
            self._cancel_timers()
            self._revert_all()
            self._stopped = True
            self._cond.notify_all()
        self._thread.join(timeout=5)

    def _loop(self) -> None:
        with self._cond:
            while not self._stopped:
                if not self._queue:
                    self._cond.wait()
                    continue
                due, token, message = self._queue[0]
                delay = due - time.monotonic()
                if delay > 0:
                    self._cond.wait(delay)
                    continue
                heapq.heappop(self._queue)
                if token not in self._live:
                    continue
                self._live.discard(token)
                try:
                    self._handle_info(message)
                except Exception:
                    logger.exception("Flag scheduler failed to handle %s", message[0])

    def _send_after(self, delay_ms: int, message: tuple) -> int:
        token = next(self._seq)
        self._live.add(token)
        heapq.heappush(self._queue, (time.monotonic() + delay_ms / 1000, token, message))
        self._cond.notify_all()
        return token

    def _handle_info(self, message: tuple) -> None:
        kind, epoch = message[0], message[1]
        if not self._running or epoch != self._epoch:
            return
        if kind == "begin_interval":
            self._timers.pop("interval", None)
            self._begin_interval()
            self._broadcast()
        elif kind == "trigger":
            _, _, flag, variant, resting_variant, duration_ms = message
            self._handle_trigger(epoch, flag, variant, resting_variant, duration_ms)
        elif kind == "revert":
            flag = message[2]
            self._timers.pop(("revert", flag), None)
            if flag in self._active:
                self._revert_flag(flag)
                self._finish_activation(flag)
            self._broadcast()

    def _handle_trigger(self, epoch, flag, variant, resting_variant, duration_ms) -> None:
        # The same flag can be picked again while a previous hold is still running,
        # so retire that activation before this one takes the flag over.
        self._timers.pop(("trigger", flag), None)
        self._cancel_timer(("revert", flag))
        self._finish_activation(flag)

        self._storage.write(flag, variant)
        logger.info("Flag scheduler set %s to %s for %sms", flag, variant, duration_ms)

        self._timers[("revert", flag)] = self._send_after(duration_ms, ("revert", epoch, flag))
        self._active[flag] = {
            "flag": flag,
            "variant": variant,
            "resting_variant": resting_variant,
            "duration_ms": duration_ms,
            "started_at": datetime.now(UTC),
            "until": monotonic_ms() + duration_ms,
        }
        self._next_up = [pending for pending in self._next_up if pending["flag"] != flag]
        self._broadcast()

    def _begin_interval(self) -> None:
        config = self._config
        self._timers["interval"] = self._send_after(
            config["interval_ms"], ("begin_interval", self._epoch)
        )
        self._interval_count += 1

        flags = self._eligible_flags()
        wanted = min(config["concurrency"], len(flags))
        self._next_up = []
        if wanted == 0:
            logger.warning("Flag scheduler has no eligible flags selected, skipping interval")
            return

        if config["concurrency"] > wanted:
            logger.warning(
                "Flag scheduler was asked for %s concurrent flags but only %s are selected",
                config["concurrency"],
                wanted,
            )

        for plan in self._plan_activations(flags, wanted):
            self._schedule_activation(plan)

    def _plan_activations(self, flags, wanted) -> list[dict[str, Any]]:
        config = self._config
        rand = self._rand
        assert rand is not None
        remaining = list(flags)
        plans = []
        while wanted > 0 and remaining:
            picked = remaining[rand.randrange(len(remaining))]
            flag, resting_variant, variants = picked
            duration_ms = _random_in_range(
                rand, config["min_duration_ms"], config["max_duration_ms"]
            )
            offset_ms = _random_in_range(rand, 0, config["interval_ms"] - duration_ms)
            variant = variants[rand.randrange(len(variants))]
            plans.append(
                {
                    "flag": flag,
                    "variant": variant,
                    "resting_variant": resting_variant,
                    "duration_ms": duration_ms,
                    "offset_ms": offset_ms,
                }
            )
            remaining.remove(picked)
            wanted -= 1
        return plans

    def _schedule_activation(self, plan: dict[str, Any]) -> None:
        flag = plan["flag"]
        self._timers[("trigger", flag)] = self._send_after(
            plan["offset_ms"],
            (
                "trigger",
                self._epoch,
                flag,
                plan["variant"],
                plan["resting_variant"],
                plan["duration_ms"],
            ),
        )
        self._next_up.append(
            {
                "flag": flag,
                "variant": plan["variant"],
                "duration_ms": plan["duration_ms"],
                "at": monotonic_ms() + plan["offset_ms"],
            }
        )

    def _eligible_flags(self) -> list[tuple[str, Any, list[str]]]:
        selection = self._config["flags"]
        if selection == ALL:
            return list(self._eligible)
        if not isinstance(selection, dict):
            return []
        eligible = []
        for name, resting, variants in self._eligible:
            chosen = selection.get(name) or []
            allowed = [variant for variant in variants if variant in chosen]
            if allowed:
                eligible.append((name, resting, allowed))
        return eligible

    def _revert_all(self) -> None:
        for flag in list(self._active):
            self._revert_flag(flag)
            self._finish_activation(flag)

    def _revert_flag(self, flag: str) -> None:
        variant = self._active[flag]["resting_variant"]
        self._storage.write(flag, variant)
        logger.info("Flag scheduler reverted %s to %s", flag, variant)

    def _finish_activation(self, flag: str) -> None:
        activation = self._active.pop(flag, None)
        if activation is not None:
            self._history = [activation, *self._history][:HISTORY_LIMIT]

    def _cancel_timers(self) -> None:
        for token in self._timers.values():
            self._live.discard(token)
        self._timers = {}

    def _cancel_timer(self, key: Any) -> None:
        token = self._timers.pop(key, None)
        if token is not None:
            self._live.discard(token)

    def _public_state(self) -> dict[str, Any]:
        next_up = [dict(pending) for pending in self._next_up]
        return {
            "config": dict(self._config),
            "running": self._running,
            "seed_used": self._seed_used,
            "interval_count": self._interval_count,
            "next_up": next_up,
            "history": [dict(entry) for entry in self._history],
            "active": [dict(self._active[flag]) for flag in sorted(self._active)],
            "next_trigger_at": min((p["at"] for p in next_up), default=None),
        }

    def _broadcast(self) -> None:
        self._pubsub.broadcast(TOPIC, ("scheduler_state", self._public_state()))


def _random_in_range(rand: random.Random, low: int, high: int) -> int:
    if high > low:
        return low + rand.randrange(high - low + 1)
    return low
