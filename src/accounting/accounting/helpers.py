# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import json
import re
from collections.abc import Iterable, Mapping

from google.protobuf import json_format

RELEVANT_PREFIXES = ["DOTNET_", "CORECLR_", "OTEL_", "KAFKA_"]

# Matches API, TOKEN, SECRET, KEY, PASSWORD, PASS, PWD, HEADER(S) or CREDENTIALS as a whole
# underscore-delimited word of an environment variable name, case-insensitively. Same pattern
# the OpenTelemetry .NET auto-instrumentation uses to hide secrets when it logs environment variables.
SECRETS_PATTERN = re.compile(
    r"(?:^|_)(API|TOKEN|SECRET|KEY|PASSWORD|PASS|PWD|HEADERS?|CREDENTIALS)(?:_|=|$)",
    re.IGNORECASE,
)


def filter_relevant(envs: Mapping[str, str]) -> Iterable[tuple[str, str]]:
    for key, value in envs.items():
        for prefix in RELEVANT_PREFIXES:
            if key.upper().startswith(prefix):
                yield key, value


def _culture_sort_key(key: str) -> tuple[str, str]:
    return key.casefold().replace("_", " "), key.swapcase()


def is_sensitive(key: str | None) -> bool:
    return key is not None and SECRETS_PATTERN.search(key) is not None


def output_in_order(envs: Iterable[tuple[str, str]], write=print) -> None:
    for key, value in sorted(envs, key=lambda kv: _culture_sort_key(kv[0])):
        write(f"[{key}, {'[REDACTED]' if is_sensitive(key) else value}]")


def _diagnostic_json(value) -> str:
    if isinstance(value, dict):
        if not value:
            return "{ }"
        return "{ " + ", ".join(f"{json.dumps(k)}: {_diagnostic_json(v)}" for k, v in value.items()) + " }"
    if isinstance(value, list):
        if not value:
            return "[ ]"
        return "[ " + ", ".join(_diagnostic_json(v) for v in value) + " ]"
    return json.dumps(value, ensure_ascii=False)


def to_diagnostic_string(message) -> str:
    return _diagnostic_json(json_format.MessageToDict(message))
