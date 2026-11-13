# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import os
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class IntResolution:
    value: int
    variant: str | None = None
    error: str | None = None


class FlagEvaluator(Protocol):
    def resolve_int(self, key: str, default: int) -> IntResolution: ...


class OpenFeatureFlags:
    def __init__(self):
        from openfeature import api
        from openfeature.contrib.provider.flagd import FlagdProvider
        from openfeature.contrib.provider.flagd.config import CacheType

        api.set_provider(
            FlagdProvider(
                host=os.environ.get("FLAGD_HOST", "localhost"),
                port=int(os.environ.get("FLAGD_PORT", "8013")),
                cache=CacheType.DISABLED,
            )
        )
        self._client = api.get_client()

    def resolve_int(self, key: str, default: int) -> IntResolution:
        details = self._client.get_integer_details(key, default)
        if details.error_code is not None:
            return IntResolution(default, error=f"{details.error_code}: {details.error_message}")
        return IntResolution(details.value, details.variant)
