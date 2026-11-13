# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import json
import re
from typing import Any
from urllib.parse import parse_qsl

from starlette.requests import Request
from starlette.responses import Response

JSON_CONTENT_TYPE = "application/json; charset=utf-8"
BODY_SIZE_LIMIT = 1024 * 1024

# Matches the "no-cache" directive anywhere in a Cache-Control header value.
_NO_CACHE = re.compile(r"(?:^|,)\s*?no-cache\s*?(?:,|$)")


class ApiError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _utf16_units(text: str) -> list[int]:
    raw = text.encode("utf-16-le")
    return [raw[i] | (raw[i + 1] << 8) for i in range(0, len(raw), 2)]


def _fnv1a52(units: list[int]) -> int:
    v0, v1, v2, v3 = 0x2325, 0x8422, 0x9CE4, 0xCBF2
    for code in units:
        v0 ^= code
        t0 = v0 * 435
        t1 = v1 * 435
        t2 = v2 * 435
        t3 = v3 * 435
        t2 += v0 << 8
        t3 += v1 << 8
        t1 += t0 >> 16
        v0 = t0 & 65535
        t2 += t1 >> 16
        v1 = t1 & 65535
        v3 = (t3 + (t2 >> 16)) & 65535
        v2 = t2 & 65535
    return (v3 & 15) * 281474976710656 + v2 * 4294967296 + v1 * 65536 + (v0 ^ (v3 >> 4))


def _base36(number: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if number == 0:
        return "0"
    out = []
    while number:
        number, rem = divmod(number, 36)
        out.append(digits[rem])
    return "".join(reversed(out))


def generate_etag(payload: str) -> str:
    units = _utf16_units(payload)
    return f'"{_base36(_fnv1a52(units))}{_base36(len(units))}"'


def _parse_token_list(value: str) -> list[str]:
    tokens = []
    start = end = 0
    for i, ch in enumerate(value):
        if ch == " ":
            if start == end:
                start = end = i + 1
        elif ch == ",":
            tokens.append(value[start:end])
            start = end = i + 1
        else:
            end = i + 1
    tokens.append(value[start:end])
    return tokens


def is_fresh(request: Request, etag: str) -> bool:
    modified_since = request.headers.get("if-modified-since")
    none_match = request.headers.get("if-none-match")
    if not modified_since and not none_match:
        return False
    cache_control = request.headers.get("cache-control")
    if cache_control and _NO_CACHE.search(cache_control):
        return False
    if none_match and none_match != "*":
        if not any(t == etag or t == "W/" + etag or "W/" + t == etag for t in _parse_token_list(none_match)):
            return False
    if modified_since:
        return False
    return True


def js_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def send(request: Request, status: int, body: str, content_type: str | None = None) -> Response:
    if status in (204, 304):
        return Response(status_code=status)
    etag = generate_etag(body)
    headers = {"ETag": etag}
    if is_fresh(request, etag):
        return Response(status_code=304, headers=headers)
    if content_type:
        headers["Content-Type"] = content_type
    return Response(content=body.encode(), status_code=status, headers=headers)


def send_json(request: Request, status: int, data: Any) -> Response:
    return send(request, status, js_json(data), JSON_CONTENT_TYPE)


def send_error(status: int, message: str) -> Response:
    return Response(content=message.encode(), status_code=status)


def query_value(request: Request, key: str) -> str | list[str] | None:
    values = request.query_params.getlist(key)
    if not values:
        return None
    return values[0] if len(values) == 1 else values


def query_string(request: Request, key: str, default: str = "") -> str:
    value = query_value(request, key)
    if value is None:
        return default
    return value if isinstance(value, str) else value[0]


async def parse_body(request: Request) -> Any:
    raw_type = request.headers.get("content-type") or "text/plain"
    media_type = raw_type.split(";", 1)[0].strip().lower()
    charset = "utf-8"
    for param in raw_type.split(";")[1:]:
        name, _, value = param.partition("=")
        if name.strip().lower() == "charset" and value.strip():
            charset = value.strip().strip('"')
    chunks = bytearray()
    async for chunk in request.stream():
        chunks.extend(chunk)
        if len(chunks) > BODY_SIZE_LIMIT:
            raise ApiError(413, "Body exceeded 1mb limit")
    try:
        text = bytes(chunks).decode(charset)
    except (LookupError, UnicodeDecodeError):
        raise ApiError(400, "Invalid body")
    if media_type in ("application/json", "application/ld+json"):
        if not text:
            return {}
        try:
            return json.loads(text)
        except ValueError:
            raise ApiError(400, "Invalid JSON")
    if media_type == "application/x-www-form-urlencoded":
        form: dict[str, Any] = {}
        for key, value in parse_qsl(text, keep_blank_values=True):
            if key in form:
                form[key] = [*form[key], value] if isinstance(form[key], list) else [form[key], value]
            else:
                form[key] = value
        return form
    return text


def field(body: Any, key: str) -> Any:
    return body.get(key) if isinstance(body, dict) else None
