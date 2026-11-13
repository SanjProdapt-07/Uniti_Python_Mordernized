#!/usr/bin/python
# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import json

import pytest

AD_FLAGS = {
    "adFailure": {
        "defaultVariant": "off",
        "description": "Fail ad service",
        "state": "ENABLED",
        "variants": {"off": False, "on": True},
    },
    "adHighCpu": {
        "defaultVariant": "off",
        "description": "Triggers high cpu load in the ad service",
        "state": "ENABLED",
        "variants": {"off": False, "on": True},
    },
}

SWAPPED_FLAGS = {
    name: {**data, "variants": {"off": True, "on": False}} for name, data in AD_FLAGS.items()
}


@pytest.mark.parametrize("path", ["/api/read", "/api/read-file"])
def test_read(client, path):
    response = client.get(path)

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json; charset=utf-8"
    flags = response.json()["flags"]
    assert {name: flags[name] for name in AD_FLAGS} == AD_FLAGS


@pytest.mark.parametrize(("write", "read"), [("/api/write", "/api/read"), ("/api/write-to-file", "/api/read-file")])
def test_write_replaces_the_whole_configuration(client, flag_file, write, read):
    response = client.post(write, json={"data": {"flags": SWAPPED_FLAGS}})

    assert response.status_code == 200
    assert response.json() == {}
    assert client.get(read).json() == {"flags": SWAPPED_FLAGS}
    assert flag_file.read_text() == json.dumps(
        {"flags": SWAPPED_FLAGS}, separators=(",", ":"), sort_keys=True
    )


def test_works_behind_the_envoy_prefix_rewrite(client):
    assert client.get("/feature/api/read").status_code == 200
    assert client.get("http://testserver//api/read").status_code == 200


def test_write_without_data_is_a_bad_request(client):
    response = client.post("/api/write", json={"other": 1})

    assert response.status_code == 400
    assert response.json() == {"errors": {"detail": "Bad Request"}}


def test_write_with_malformed_json_is_a_bad_request(client, flag_file):
    before = flag_file.read_text()
    response = client.post("/api/write", content=b"{oops", headers={"content-type": "application/json"})

    assert response.status_code == 400
    assert flag_file.read_text() == before


def test_write_accepts_form_encoded_nested_params(client):
    response = client.post("/api/write", data={"data[flags][x][defaultVariant]": "on"})

    assert response.status_code == 200
    assert client.get("/api/read").json() == {"flags": {"x": {"defaultVariant": "on"}}}


def test_read_without_flags_is_an_internal_error(client):
    client.post("/api/write", json={"data": {"$schema": "x"}})

    response = client.get("/api/read")

    assert response.status_code == 500
    assert response.json() == {"errors": {"detail": "Internal Server Error"}}


def test_unknown_route_renders_not_found(client):
    html = client.get("/nope")
    assert (html.status_code, html.text) == (404, "Not Found")

    json_response = client.get("/nope", headers={"accept": "application/json"})
    assert json_response.json() == {"errors": {"detail": "Not Found"}}

    assert client.get("/api/write").status_code == 404


def test_api_rejects_an_unacceptable_format(client):
    assert client.get("/api/read", headers={"accept": "text/html"}).status_code == 406
