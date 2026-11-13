# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import asyncio

from uvicorn.protocols.http.h11_impl import H11Protocol


class Stats:
    def __init__(self) -> None:
        self.accepted = 0
        self.handled = 0
        self.active = 0
        self.requests = 0
        self.reading = 0
        self.writing = 0

    def render(self) -> bytes:
        active = max(self.active, self.writing)
        waiting = max(active - self.reading - self.writing, 0)
        return (
            f"Active connections: {active} \n"
            "server accepts handled requests\n"
            f" {self.accepted} {self.handled} {self.requests} \n"
            f"Reading: {self.reading} Writing: {self.writing} Waiting: {waiting} \n"
        ).encode()


def counting_protocol(stats: Stats) -> type[H11Protocol]:
    class CountingH11Protocol(H11Protocol):
        def connection_made(self, transport: asyncio.Transport) -> None:  # type: ignore[override]
            stats.accepted += 1
            stats.handled += 1
            stats.active += 1
            super().connection_made(transport)

        def connection_lost(self, exc: Exception | None) -> None:
            stats.active -= 1
            super().connection_lost(exc)

    return CountingH11Protocol
