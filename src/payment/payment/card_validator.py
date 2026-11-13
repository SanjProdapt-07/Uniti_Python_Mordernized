# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

"""Port of the card-number checks of simple-card-validator 1.1.0 (getCardDetails)."""

import re
import unicodedata
from dataclasses import dataclass

# Each pattern is anchored at the start of the normalized card number and
# matches the issuer prefix (IIN) of the named scheme.
_CARD_TYPES = (
    {"name": "amex", "pattern": re.compile(r"^3[47]"), "valid_length": (15,)},
    {"name": "diners_club_carte_blanche", "pattern": re.compile(r"^30[0-5]"), "valid_length": (14,)},
    {"name": "diners_club_international", "pattern": re.compile(r"^3([689]|09)"), "valid_length": (14,)},
    {"name": "jcb", "pattern": re.compile(r"^35(2[89]|[3-8][0-9])"), "valid_length": (16,)},
    {"name": "laser", "pattern": re.compile(r"^(6304|670[69]|6771)"), "valid_length": (16, 17, 18, 19)},
    {"name": "visa_electron", "pattern": re.compile(r"^(4026|417500|4508|4844|491(3|7))"), "valid_length": (16,)},
    {"name": "visa", "pattern": re.compile(r"^4"), "valid_length": (16,)},
    {"name": "mastercard", "pattern": re.compile(r"^5[1-5]"), "valid_length": (16,)},
    {
        "name": "maestro",
        "pattern": re.compile(r"^(5018|5081|5044|5020|5038|603845|6304|6759|676[1-3]|6220|504834|504817|504645)"),
        "valid_length": (12, 13, 14, 15, 16, 17, 18, 19),
    },
    {
        "name": "rupay",
        "range": (
            (508500, 508999),
            (606985, 607384),
            (607385, 607484),
            (607485, 607984),
            (608001, 608100),
            (608101, 608200),
            (608201, 608300),
            (608301, 608350),
            (608351, 608500),
            (652150, 652849),
            (652850, 653049),
            (653050, 653149),
            (817200, 819899),
            (819900, 820199),
        ),
        "valid_length": (16,),
    },
    {
        "name": "discover",
        "pattern": re.compile(r"^(6011|622(12[6-9]|1[3-9][0-9]|[2-8][0-9]{2}|9[0-1][0-9]|92[0-5]|64[4-9])|65)"),
        "valid_length": (16,),
    },
)

_JS_LINE_TERMINATORS = "\n\r  "
_JS_EXTRA_WHITESPACE = "\t\v\f﻿"

# A JavaScript StringNumericLiteral: optional sign with decimal digits, optional
# fraction and exponent, or Infinity; or an unsigned 0x/0o/0b integer literal.
_JS_NUMERIC = re.compile(
    r"[+-]?(?:(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?|Infinity)|0[xX][0-9a-fA-F]+|0[oO][0-7]+|0[bB][01]+",
    re.ASCII,
)
# JavaScript parseInt prefix: optional sign then either a hex literal or decimal digits.
_JS_PARSE_INT = re.compile(r"([+-]?)(?:0[xX]([0-9a-fA-F]+)|(\d+))", re.ASCII)


def _is_js_whitespace(ch: str) -> bool:
    return ch in _JS_LINE_TERMINATORS or ch in _JS_EXTRA_WHITESPACE or unicodedata.category(ch) == "Zs"


def _js_trim(value: str) -> str:
    start, end = 0, len(value)
    while start < end and _is_js_whitespace(value[start]):
        start += 1
    while end > start and _is_js_whitespace(value[end - 1]):
        end -= 1
    return value[start:end]


def _js_is_nan(value: str) -> bool:
    trimmed = _js_trim(value)
    return trimmed != "" and _JS_NUMERIC.fullmatch(trimmed) is None


def _js_parse_int(value: str) -> int | None:
    match = _JS_PARSE_INT.match(_js_trim(value))
    if match is None:
        return None
    sign = -1 if match.group(1) == "-" else 1
    return sign * (int(match.group(2), 16) if match.group(2) else int(match.group(3)))


def _js_digit(ch: str) -> int | None:
    if "0" <= ch <= "9":
        return ord(ch) - 48
    if _is_js_whitespace(ch):
        return 0
    return None


def _card_type(number: str):
    for card in _CARD_TYPES:
        if "pattern" in card:
            if card["pattern"].search(number):
                return card
        else:
            bin_prefix = _utf16_slice(number, 6)
            if _js_is_nan(bin_prefix):
                return None
            bin_value = _js_parse_int(bin_prefix)
            if bin_value is not None:
                for low, high in card["range"]:
                    if low <= bin_value <= high:
                        return card
    return None


def _utf16_units(value: str) -> list[str]:
    units = []
    for ch in value:
        if ord(ch) > 0xFFFF:
            code = ord(ch) - 0x10000
            units.append(chr(0xD800 + (code >> 10)))
            units.append(chr(0xDC00 + (code & 0x3FF)))
        else:
            units.append(ch)
    return units


def _utf16_slice(value: str, count: int) -> str:
    return "".join(_utf16_units(value)[:count])


def _luhn_valid(number: str) -> bool:
    total = 0
    for index, ch in enumerate(reversed(_utf16_units(number))):
        digit = _js_digit(ch)
        if digit is None:
            return False
        if index % 2:
            digit *= 2
            total += digit if digit < 10 else digit - 9
        else:
            total += digit
    return total % 10 == 0


@dataclass(frozen=True)
class CardDetails:
    card_type: str
    valid: bool


class InvalidCardNumber(Exception):
    pass


def get_card_details(card_number: str) -> CardDetails:
    if not card_number:
        raise InvalidCardNumber("Invalid cardNumber property set")
    number = card_number.replace(" ", "").replace("-", "")
    card = _card_type(number)
    if card is None:
        return CardDetails(card_type="unknown", valid=False)
    length_valid = len(_utf16_units(number)) in card["valid_length"]
    return CardDetails(card_type=card["name"], valid=_luhn_valid(number) and length_valid)
