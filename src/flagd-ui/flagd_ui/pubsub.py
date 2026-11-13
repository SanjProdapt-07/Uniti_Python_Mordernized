#!/usr/bin/python
# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import logging
import threading
from collections import defaultdict
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class PubSub:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: dict[str, list[Callable[[Any], None]]] = defaultdict(list)

    def subscribe(self, topic: str, callback: Callable[[Any], None]) -> Callable[[], None]:
        with self._lock:
            self._subscribers[topic].append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers[topic]:
                    self._subscribers[topic].remove(callback)

        return unsubscribe

    def broadcast(self, topic: str, message: Any) -> None:
        with self._lock:
            callbacks = list(self._subscribers[topic])
        for callback in callbacks:
            try:
                callback(message)
            except Exception:
                logger.exception("PubSub subscriber failed on topic %s", topic)
