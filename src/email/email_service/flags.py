# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import os
from typing import Protocol


class FlagEvaluator(Protocol):
    def number(self, key: str, default: int) -> int: ...


class OpenFeatureFlags:
    def __init__(self):
        from openfeature import api
        from openfeature.contrib.provider.flagd import FlagdProvider
        from openfeature.contrib.provider.flagd.config import CacheType

        api.set_provider(
            FlagdProvider(
                host=os.environ.get("FLAGD_HOST", "localhost"),
                port=int(os.environ.get("FLAGD_PORT", "8013")),
                tls=os.environ.get("FLAGD_TLS", "false") == "true",
                cache=CacheType.DISABLED,
            )
        )
        self._api = api

    def number(self, key: str, default: int) -> int:
        return self._api.get_client().get_integer_value(key, default)
