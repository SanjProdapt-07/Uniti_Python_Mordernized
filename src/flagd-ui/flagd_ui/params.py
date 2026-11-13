#!/usr/bin/python
# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

"""Decodes flat ``key[sub][]=value`` pairs into nested params the way Plug does."""

import re
from collections.abc import Iterable
from typing import Any

# Captures the content of each "[...]" segment of a key, e.g. "a[b][]" -> ["b", ""].
_SEGMENT = re.compile(r"\[([^\[\]]*)\]")


def _split_key(key: str) -> list[str]:
    bracket = key.find("[")
    if bracket <= 0:
        return [key]
    return [key[:bracket], *_SEGMENT.findall(key[bracket:])]


def _assign(target: dict[str, Any], parts: list[str], value: Any) -> None:
    head, rest = parts[0], parts[1:]
    if not rest:
        target[head] = value
        return
    if rest == [""]:
        current = target.get(head)
        if not isinstance(current, list):
            current = []
            target[head] = current
        current.append(value)
        return
    child = target.get(head)
    if not isinstance(child, dict):
        child = {}
        target[head] = child
    _assign(child, rest, value)


def decode_pairs(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for key, value in pairs:
        if key == "":
            continue
        _assign(params, _split_key(key), value)
    return params


def flatten_values(values: dict[str, Any]) -> list[tuple[str, Any]]:
    pairs: list[tuple[str, Any]] = []
    for key, value in values.items():
        if isinstance(value, list):
            pairs.extend((key, item) for item in value)
        else:
            pairs.append((key, value))
    return pairs
