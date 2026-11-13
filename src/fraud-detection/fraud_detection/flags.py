# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import uuid
from typing import Protocol

from openfeature import api
from openfeature.evaluation_context import EvaluationContext


class FlagClient(Protocol):
    def get_integer_value(self, flag_key: str, default_value: int, evaluation_context=None) -> int: ...
    def get_boolean_value(self, flag_key: str, default_value: bool, evaluation_context=None) -> bool: ...


def init_provider() -> None:
    from openfeature.contrib.provider.flagd import FlagdProvider

    api.set_provider(FlagdProvider())


def _session_context() -> EvaluationContext:
    return EvaluationContext(attributes={"session": str(uuid.uuid4())})


class FeatureFlags:
    def __init__(self, client: FlagClient | None = None):
        self._client = client

    @property
    def client(self) -> FlagClient:
        return self._client if self._client is not None else api.get_client()

    def get_feature_flag_value(self, ff: str) -> int:
        return self.client.get_integer_value(ff, 0, _session_context())

    def get_boolean_feature_flag_value(self, ff: str) -> bool:
        return self.client.get_boolean_value(ff, False, _session_context())
