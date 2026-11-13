# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import demo_pb2

NANOS_MIN = -999999999
NANOS_MAX = +999999999
NANOS_MOD = 1000000000


class MoneyError(ValueError):
    pass


class InvalidValueError(MoneyError):
    def __init__(self):
        super().__init__("one of the specified money values is invalid")


class MismatchingCurrencyError(MoneyError):
    def __init__(self):
        super().__init__("mismatching currency codes")


def _trunc_div(a, b):
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b >= 0) else -q


def _trunc_mod(a, b):
    return a - b * _trunc_div(a, b)


def _wrap(v, bits):
    v &= (1 << bits) - 1
    return v - (1 << bits) if v >> (bits - 1) else v


def is_valid(m):
    return _sign_matches(m) and _valid_nanos(m.nanos)


def _sign_matches(m):
    return m.nanos == 0 or m.units == 0 or (m.nanos < 0) == (m.units < 0)


def _valid_nanos(nanos):
    return NANOS_MIN <= nanos <= NANOS_MAX


def is_zero(m):
    return m.units == 0 and m.nanos == 0


def is_positive(m):
    return (is_valid(m) and m.units > 0) or (m.units == 0 and m.nanos > 0)


def is_negative(m):
    return (is_valid(m) and m.units < 0) or (m.units == 0 and m.nanos < 0)


def are_same_currency(l, r):
    return l.currency_code == r.currency_code and l.currency_code != ""


def are_equals(l, r):
    return l.currency_code == r.currency_code and l.units == r.units and l.nanos == r.nanos


def negate(m):
    return demo_pb2.Money(units=-m.units, nanos=-m.nanos, currency_code=m.currency_code)


def sum(l, r):
    if not is_valid(l) or not is_valid(r):
        raise InvalidValueError()
    if l.currency_code != r.currency_code:
        raise MismatchingCurrencyError()
    units = _wrap(l.units + r.units, 64)
    nanos = _wrap(l.nanos + r.nanos, 32)

    if (units == 0 and nanos == 0) or (units > 0 and nanos >= 0) or (units < 0 and nanos <= 0):
        units += _trunc_div(nanos, NANOS_MOD)
        nanos = _trunc_mod(nanos, NANOS_MOD)
    elif units > 0:
        units -= 1
        nanos += NANOS_MOD
    else:
        units += 1
        nanos -= NANOS_MOD

    return demo_pb2.Money(units=_wrap(units, 64), nanos=_wrap(nanos, 32), currency_code=l.currency_code)


def multiply_slow(m, n):
    n &= 0xFFFFFFFF
    out = m
    while n > 1:
        out = sum(out, m)
        n -= 1
    return out
