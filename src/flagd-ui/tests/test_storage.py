#!/usr/bin/python
# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import json

from flagd_ui.storage import Storage


def test_starts_with_the_file_content(storage, flag_file):
    assert storage.read() == json.loads(flag_file.read_text())


def test_a_second_storage_on_the_same_file_reads_the_same_state(storage, flag_file, pubsub):
    assert Storage(str(flag_file), pubsub).read() == storage.read()


def test_missing_empty_or_invalid_file_starts_empty(tmp_path, pubsub):
    assert Storage(str(tmp_path / "missing.json"), pubsub).read() == {}
    (tmp_path / "empty.json").write_text("")
    assert Storage(str(tmp_path / "empty.json"), pubsub).read() == {}
    (tmp_path / "bad.json").write_text("{nope")
    assert Storage(str(tmp_path / "bad.json"), pubsub).read() == {}


def test_replace_with_invalid_json_keeps_prior_state_and_file_content(storage, flag_file):
    state_before = storage.read()
    content_before = flag_file.read_text()

    storage.replace("not valid json")

    assert storage.read() == state_before
    assert flag_file.read_text() == content_before


def test_replace_writes_the_given_text_verbatim_and_broadcasts(storage, flag_file, inbox):
    messages = inbox("flags")
    storage.replace('{"flags": {}}')

    assert flag_file.read_text() == '{"flags": {}}'
    assert storage.read() == {"flags": {}}
    assert messages.receive(lambda m: m[0] == "flags_changed")[1] == {"flags": {}}


def test_write_sets_default_variant_and_writes_sorted_pretty_json(storage, flag_file):
    storage.write("adFailure", "on")

    content = flag_file.read_text()
    state = json.loads(content)
    assert state["flags"]["adFailure"]["defaultVariant"] == "on"
    assert content == json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False)
    assert not flag_file.with_name(flag_file.name + ".tmp").exists()


def test_write_flips_the_targeting_rule_instead_of_the_default(storage):
    storage.write("productCatalogFailure", "on")

    flag = storage.read()["flags"]["productCatalogFailure"]
    assert flag["targeting"]["if"][1] == "on"
    assert flag["defaultVariant"] == "off"


def test_write_of_an_unknown_flag_leaves_flags_untouched(storage):
    before = storage.read()["flags"]
    storage.write("doesNotExist", "on")
    assert storage.read()["flags"] == before


def test_write_without_flags_creates_an_empty_flags_object(tmp_path, pubsub):
    path = tmp_path / "noflags.json"
    path.write_text('{"$schema": "x"}')
    store = Storage(str(path), pubsub)
    store.write("adFailure", "on")
    assert json.loads(path.read_text()) == {"$schema": "x", "flags": {}}
