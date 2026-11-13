# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import demo_pb2
import pytest

from checkout import money


def mmc(u, n, c):
    return demo_pb2.Money(units=u, nanos=n, currency_code=c)


def mm(u, n):
    return mmc(u, n, "")


@pytest.mark.parametrize("m,want", [
    (mm(-981273891273, -999999999), True),
    (mm(-981273891273, +999999999), False),
    (mm(981273891273, 999999999), True),
    (mm(981273891273, -999999999), False),
    (mm(3, 1000000000), False),
    (mm(3, -1000000000), False),
    (mm(-3, 1000000000), False),
    (mm(-3, -1000000000), False),
])
def test_is_valid(m, want):
    assert money.is_valid(m) is want


@pytest.mark.parametrize("m,want", [
    (mm(0, 0), True), (mm(-1, +1), False), (mm(-1, -1), False), (mm(+1, +1), False), (mm(+1, -1), False),
])
def test_is_zero(m, want):
    assert money.is_zero(m) is want


@pytest.mark.parametrize("m,want", [
    (mm(0, 0), False), (mm(+1, +1), True), (mm(-1, +1), False), (mm(-1, -1), False), (mm(+1, -1), False),
])
def test_is_positive(m, want):
    assert money.is_positive(m) is want


@pytest.mark.parametrize("m,want", [
    (mm(0, 0), False), (mm(+1, +1), False), (mm(-1, +1), False), (mm(-1, -1), True), (mm(+1, -1), False),
])
def test_is_negative(m, want):
    assert money.is_negative(m) is want


@pytest.mark.parametrize("l,r,want", [
    (mmc(1, 0, ""), mmc(2, 0, ""), False),
    (mmc(1, 0, ""), mmc(2, 0, "USD"), False),
    (mmc(1, 0, "USD"), mmc(2, 0, ""), False),
    (mmc(1, 0, "USD"), mmc(2, 0, "CAD"), False),
    (mmc(1, 0, "USD"), mmc(2, 0, "USD"), True),
])
def test_are_same_currency(l, r, want):
    assert money.are_same_currency(l, r) is want


@pytest.mark.parametrize("l,r,want", [
    (mmc(1, 2, "USD"), mmc(1, 2, "USD"), True),
    (mmc(1, 2, "USD"), mmc(1, 2, "CAD"), False),
    (mmc(10, 20, "USD"), mmc(1, 20, "USD"), False),
    (mmc(1, 2, "USD"), mmc(1, 20, "USD"), False),
    (mmc(1, 2, "USD"), mmc(-1, -2, "USD"), False),
])
def test_are_equals(l, r, want):
    assert money.are_equals(l, r) is want


@pytest.mark.parametrize("m,want", [
    (mm(0, 0), mm(0, 0)),
    (mm(-1, -200), mm(1, 200)),
    (mm(1, 200), mm(-1, -200)),
    (mmc(0, 0, "XXX"), mmc(0, 0, "XXX")),
])
def test_negate(m, want):
    assert money.are_equals(money.negate(m), want)


@pytest.mark.parametrize("l,r,want,err", [
    (mm(0, 0), mm(0, 0), mm(0, 0), None),
    (mmc(0, 0, "XXX"), mm(0, 0), None, money.MismatchingCurrencyError),
    (mm(0, 0), mmc(0, 0, "YYY"), None, money.MismatchingCurrencyError),
    (mmc(0, 0, "AAA"), mmc(0, 0, "BBB"), None, money.MismatchingCurrencyError),
    (mm(+1, -1), mm(0, 0), None, money.InvalidValueError),
    (mm(0, 0), mm(-1, +2), None, money.InvalidValueError),
    (mm(0, 1000000000), mm(1, 0), None, money.InvalidValueError),
    (mm(2, 200000000), mm(2, 200000000), mm(4, 400000000), None),
    (mm(2, 111111111), mm(2, 888888888), mm(4, 999999999), None),
    (mm(2, 200000000), mm(2, 900000000), mm(5, 100000000), None),
    (mm(-2, -200000000), mm(-2, -200000000), mm(-4, -400000000), None),
    (mm(-2, -200000000), mm(-2, -900000000), mm(-5, -100000000), None),
    (mm(11, 0), mm(-2, 0), mm(9, 0), None),
    (mm(-11, 0), mm(2, 0), mm(-9, 0), None),
    (mm(11, 100000000), mm(-2, -100000000), mm(9, 0), None),
    (mm(11, 100000000), mm(-2, -9000000), mm(9, 91000000), None),
    (mm(-11, -100000000), mm(2, 100000000), mm(-9, 0), None),
    (mm(-11, -100000000), mm(2, 9000000), mm(-9, -91000000), None),
    (mm(0, 0), mm(-2, -100000000), mm(-2, -100000000), None),
    (mm(-2, -100000000), mm(0, 0), mm(-2, -100000000), None),
])
def test_sum(l, r, want, err):
    if err is not None:
        with pytest.raises(err):
            money.sum(l, r)
    else:
        assert money.sum(l, r) == want


def test_sum_error_messages_match_go():
    with pytest.raises(ValueError, match="^one of the specified money values is invalid$"):
        money.sum(mm(1, -1), mm(0, 0))
    with pytest.raises(ValueError, match="^mismatching currency codes$"):
        money.sum(mmc(0, 0, "A"), mm(0, 0))


def test_sum_keeps_go_quirk_for_zero_units_positive_nanos():
    assert money.sum(mm(0, 0), mm(0, 500000000)) == mm(1, -500000000)


def test_sum_wraps_nanos_like_int32():
    assert money.sum(mm(0, -999999999), mm(0, -999999999)) == mm(1, 1294967298)


@pytest.mark.parametrize("m,n,want", [
    (mmc(1, 500000000, "USD"), 3, mmc(4, 500000000, "USD")),
    (mmc(1, 500000000, "USD"), 1, mmc(1, 500000000, "USD")),
    (mmc(1, 500000000, "USD"), 0, mmc(1, 500000000, "USD")),
])
def test_multiply_slow(m, n, want):
    assert money.multiply_slow(m, n) == want
