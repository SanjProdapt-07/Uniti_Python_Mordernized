# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import demo_pb2
import grpc
import pytest

from checkout import compat


@pytest.mark.parametrize("seconds,want", [
    (0, "0s"),
    (0.000000500, "500ns"),
    (0.0000015, "1.5µs"),
    (0.001234567, "1.234567ms"),
    (0.25, "250ms"),
    (1.5, "1.5s"),
    (61, "1m1s"),
    (3723.5, "1h2m3.5s"),
])
def test_go_duration(seconds, want):
    assert compat.go_duration(seconds) == want


@pytest.mark.parametrize("units,nanos,want", [
    (8, 990000000, 8.99),
    (12, 5000000, 12.0),
    (12, 50000000, 12.05),
    (0, 0, 0.0),
    (5, -50000000, 0.0),
    (-5, -50000000, 0.0),
])
def test_money_float(units, nanos, want):
    assert compat.money_float(demo_pb2.Money(units=units, nanos=nanos)) == want


def test_go_quote():
    assert compat.go_quote("OLJCESPC7Z") == '"OLJCESPC7Z"'
    assert compat.go_quote('a"b\\c\n\x01') == '"a\\"b\\\\c\\n\\x01"'


class _Err(grpc.RpcError):
    def code(self):
        return grpc.StatusCode.UNAVAILABLE

    def details(self):
        return "connection refused"


def test_rpc_error_uses_go_status_string():
    assert compat.rpc_error(_Err()) == "rpc error: code = Unavailable desc = connection refused"


def test_go_json_matches_encoding_json():
    item = demo_pb2.CartItem(product_id="X", quantity=0)
    money = demo_pb2.Money(currency_code="USD", units=0, nanos=5)
    payload = {"items": [compat.proto_to_go_json(item)], "address": None, "m": compat.proto_to_go_json(money)}
    assert compat.go_json_dumps(payload) == b'{"address":null,"items":[{"product_id":"X"}],"m":{"currency_code":"USD","nanos":5}}'
    assert compat.go_json_dumps({"b": {"z": 1, "a": 2}, "a": 0}) == b'{"a":0,"b":{"z":1,"a":2}}'
    assert compat.go_json_dumps({"s": "<a&b>é"}) == '{"s":"\\u003ca\\u0026b\\u003eé"}'.encode()


def test_go_field_is_case_insensitive():
    assert compat.go_field({"Cost_USD": 1}, "cost_usd") == 1
    assert compat.go_field({"cost_usd": 2, "COST_USD": 1}, "cost_usd") == 2


def test_go_int_rejects_fraction_and_strings():
    with pytest.raises(compat.JSONDecodeError):
        compat.go_int(1.5, "int64", ".x")
    with pytest.raises(compat.JSONDecodeError):
        compat.go_int("1", "int64", ".x")
    with pytest.raises(compat.JSONDecodeError):
        compat.go_int(2**31, "int32", ".x")
