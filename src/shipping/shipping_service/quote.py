# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import math
import os
import re
from dataclasses import dataclass

import httpx
from opentelemetry import metrics, trace

from .logs import emit

TARGET = "shipping::shipping_service::quote"
U64_MAX = 2**64 - 1

# Float grammar accepted by Rust's f64::from_str: optional sign, then inf/infinity/nan
# (any case) or decimal digits with optional fraction and exponent.
_RUST_F64 = re.compile(r"[+-]?(?:(?i:inf|infinity|nan)|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)")

_items_shipped = metrics.get_meter("otel_demo.shipping.quote").create_counter(
    "demo.shipping.items_shipped", unit="{item}", description="Counts the number of items being shipped"
)


class QuoteError(Exception):
    pass


@dataclass(frozen=True)
class Quote:
    dollars: int
    cents: int

    def __str__(self) -> str:
        return f"{self.dollars}.{self.cents:02d}"


def _rust_round(value: float) -> float:
    whole = math.trunc(value)
    if abs(value - whole) >= 0.5:
        whole += math.copysign(1, value)
    return whole


def create_quote_from_float(value: float) -> Quote:
    scaled = value * 100.0
    if math.isnan(scaled):
        total_cents = 0
    else:
        rounded = _rust_round(scaled)
        total_cents = 0 if rounded <= 0 else U64_MAX if rounded >= 2**64 else int(rounded)
    return Quote(dollars=total_cents // 100, cents=total_cents % 100)


def _rust_debug_str(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f'"{escaped}"'


class QuoteClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self._transport = transport

    @staticmethod
    def address() -> str:
        return os.environ.get("QUOTE_ADDR", "http://quote:8090") + "/getquote"

    async def request_quote(self, count: int) -> float:
        url = self.address()
        emit(TARGET, "INFO", "Requesting quote", "shipping.quote.requested", quote_service_addr=url)
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=5.0) as client:
                response = await client.post(url, json={"numberOfItems": count})
        except httpx.HTTPError as err:
            raise QuoteError(f"Failed to call quote service: {err}") from err
        try:
            text = response.content.decode("utf-8")
        except UnicodeDecodeError as err:
            raise QuoteError("Failed to parse quote service response as UTF-8") from err
        if not _RUST_F64.fullmatch(text):
            raise QuoteError("Failed to parse quote value as f64")
        return float(text)


async def create_quote_from_count(client: QuoteClient, count: int) -> Quote:
    try:
        value = await client.request_quote(count)
    except QuoteError as err:
        raise QuoteError(f"status: 'Unknown error', self: {_rust_debug_str(str(err))}") from err

    _items_shipped.add(count)

    span = trace.get_current_span()
    quote = create_quote_from_float(value)
    if span.is_recording():
        cost = str(quote)
        span.add_event("shipping.quote.received", {"demo.shipping.cost.total": cost})
        span.set_attribute("demo.shipping.cost.total", cost)
    return quote
