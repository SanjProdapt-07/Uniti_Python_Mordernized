#!/usr/bin/python
# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import copy
import json
import logging
import os
import threading
from typing import Any

from .pubsub import PubSub

logger = logging.getLogger(__name__)

TOPIC = "flags"


def _reject_constant(name: str) -> Any:
    raise ValueError(f"invalid JSON constant {name}")


def decode(text: str) -> Any:
    return json.loads(text, parse_constant=_reject_constant)


def encode(value: Any, pretty: bool = False) -> str:
    if pretty:
        return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


def _update_flag(flags: dict, flag_name: str, value: Any) -> dict:
    updated = {}
    for name, data in flags.items():
        if name == flag_name and isinstance(data, dict):
            data = copy.deepcopy(data)
            targeting = data.get("targeting")
            rule = targeting.get("if") if isinstance(targeting, dict) else None
            if isinstance(rule, list) and len(rule) == 3:
                rule[1] = value
            elif "defaultVariant" in data:
                data["defaultVariant"] = value
        updated[name] = data
    return updated


class Storage:
    """Linearizes reads and writes of the flagd configuration file."""

    def __init__(self, file_path: str, pubsub: PubSub) -> None:
        self._file_path = file_path
        self._pubsub = pubsub
        self._lock = threading.RLock()
        self._state: Any = self._load()
        logger.info("Read new state from file")

    @property
    def file_path(self) -> str:
        return self._file_path

    def _load(self) -> Any:
        try:
            with open(self._file_path, encoding="utf-8") as handle:
                content = handle.read()
        except OSError:
            return {}
        if content == "":
            return {}
        try:
            return decode(content)
        except ValueError:
            return {}

    def read(self) -> Any:
        with self._lock:
            return copy.deepcopy(self._state)

    def replace(self, json_string: str) -> None:
        with self._lock:
            try:
                new_state = decode(json_string)
            except ValueError:
                logger.warning("Ignoring replace with invalid JSON")
                return
            if self._write_state(json_string):
                self._state = new_state
                self._broadcast()

    def write(self, flag_name: str, flag_value: Any) -> None:
        with self._lock:
            if not isinstance(self._state, dict):
                logger.error("Ignoring write of %s, the configuration is not a JSON object", flag_name)
                return
            new_state = dict(self._state)
            flags = new_state.get("flags")
            if "flags" not in new_state:
                new_state["flags"] = {}
            elif isinstance(flags, dict):
                new_state["flags"] = _update_flag(flags, flag_name, flag_value)
            else:
                logger.error("Ignoring write of %s, flags is not a JSON object", flag_name)
                return
            if self._write_state(encode(new_state, pretty=True)):
                self._state = new_state
                self._broadcast()

    def _write_state(self, json_string: str) -> bool:
        tmp_path = self._file_path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8", newline="") as handle:
                handle.write(json_string)
            os.replace(tmp_path, self._file_path)
        except OSError:
            logger.exception("Failed to write new state to file")
            return False
        logger.info("Wrote new state to file")
        return True

    def _broadcast(self) -> None:
        self._pubsub.broadcast(TOPIC, ("flags_changed", copy.deepcopy(self._state)))
