# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import math
import re
from decimal import ROUND_HALF_UP, Decimal

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1

# Leading numeric prefix PHP accepts in a string: optional whitespace, sign,
# integer or decimal digits, optional exponent.
_NUMERIC_PREFIX = re.compile(r"[ \t\n\r\v\f]*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)")
# Same prefix restricted to a plain integer (no fraction, no exponent).
_INTEGER = re.compile(r"[+-]?\d+")


def _float_to_int(value: float) -> int:
    if not math.isfinite(value) or not INT64_MIN <= value <= INT64_MAX:
        return 0
    return math.trunc(value)


def _float_to_int_capped(value: float) -> int:
    if not math.isfinite(value):
        return 0
    return max(INT64_MIN, min(INT64_MAX, math.trunc(value)))


def intval(value) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value if INT64_MIN <= value <= INT64_MAX else _float_to_int(float(value))
    if isinstance(value, float):
        return _float_to_int(value)
    if isinstance(value, str):
        match = _NUMERIC_PREFIX.match(value)
        if match is None:
            return 0
        number = match.group(1)
        if _INTEGER.fullmatch(number):
            parsed = int(number)
            if INT64_MIN <= parsed <= INT64_MAX:
                return parsed
        return _float_to_int_capped(float(number))
    if isinstance(value, (list, dict)):
        return 1 if value else 0
    return 0


def round2(value: float) -> float:
    if not math.isfinite(value):
        return value
    return float(Decimal(repr(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def json_float(value: float) -> str:
    """Encode a float the way PHP's json_encode does with serialize_precision=-1."""
    if value == 0:
        return "-0.0" if math.copysign(1.0, value) < 0 else "0.0"
    sign, digits, exponent = Decimal(repr(value)).normalize().as_tuple()
    digits_str = "".join(map(str, digits))
    decimal_exponent = len(digits_str) + exponent - 1
    prefix = "-" if sign else ""
    if decimal_exponent < -4 or decimal_exponent >= 15:
        mantissa = digits_str[0] + "." + (digits_str[1:] or "0")
        exp_sign = "-" if decimal_exponent < 0 else "+"
        return f"{prefix}{mantissa}e{exp_sign}{abs(decimal_exponent)}"
    text = repr(abs(value))
    if "e" in text or "E" in text:
        text = format(Decimal(repr(abs(value))), "f")
    if "." not in text:
        text += ".0"
    return prefix + text
