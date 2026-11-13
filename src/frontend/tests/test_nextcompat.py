# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import pytest

import demo_pb2
from frontend_server.nextcompat import generate_etag, js_json
from frontend_server.protojson import to_js


@pytest.mark.parametrize(
    ("payload", "etag"),
    [
        ("", '"78p3gz236i0"'),
        ('["USD","EUR","CHF"]', '"sywoa05vl7j"'),
        ("héllo 🔭", '"ckmlo8jh3h8"'),
    ],
)
def test_etag_matches_next_fnv1a52(payload, etag):
    assert generate_etag(payload) == etag


def test_js_json_is_compact_and_unescaped():
    assert js_json({"a": "é/<", "b": [1, 2]}) == '{"a":"é/<","b":[1,2]}'


def test_to_js_rejects_unsafe_int64():
    with pytest.raises(ValueError):
        to_js(demo_pb2.Money(units=2**53))
