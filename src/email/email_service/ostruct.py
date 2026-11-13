# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import json


class OStruct:
    """Ruby OpenStruct semantics: known keys are attributes, unknown keys read as None."""

    def __init__(self, fields: dict):
        self.__dict__.update(fields)

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)
        return None


def loads(raw: bytes | str):
    return json.loads(raw, object_hook=OStruct)
