# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
import os
import threading
import uuid

import pytest
from opentelemetry._opamp.proto import opamp_pb2

from cart.opamp import (
    CAPABILITIES,
    WebSocketTransport,
    parse_bool,
    split_attributes,
    start_opamp_client,
    websocket_url,
)
from cart.telemetry import parse_resource_attributes, prepare_environment


class FakeSocket:
    def __init__(self):
        self.connected = True
        self.sent = []
        self.received = threading.Event()

    def send(self, data, opcode):
        self.sent.append(data)
        self.received.set()

    def recv(self):
        return b"\x00" + opamp_pb2.ServerToAgent().SerializeToString()

    def close(self):
        self.connected = False


def test_parse_bool_follows_go_semantics():
    assert parse_bool("true") and parse_bool("1") and parse_bool("T")
    assert not parse_bool("false") and not parse_bool("0")
    with pytest.raises(ValueError):
        parse_bool("yes")


def test_split_attributes_identifying_keys():
    identifying, non_identifying = split_attributes(
        {"service.name": "cart", "service.instance.id": "abc", "service.namespace": "demo", "host.name": "h"}
    )
    assert identifying == {"service.name": "cart", "service.instance.id": "abc", "service.namespace": "demo"}
    assert non_identifying == {"host.name": "h"}


def test_websocket_url():
    assert websocket_url("https://opamp-server:4320/v1/opamp") == "wss://opamp-server:4320/v1/opamp"
    assert websocket_url("wss://opamp-server:4320/v1/opamp") == "wss://opamp-server:4320/v1/opamp"


def test_prepare_environment_adds_service_instance_id():
    env = {"OTEL_RESOURCE_ATTRIBUTES": "service.namespace=opentelemetry-demo"}
    instance_id = prepare_environment(env)
    attributes = parse_resource_attributes(env["OTEL_RESOURCE_ATTRIBUTES"])
    assert attributes["service.instance.id"] == instance_id
    assert attributes["service.namespace"] == "opentelemetry-demo"
    assert env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "grpc"


def test_start_sends_full_state_over_websocket():
    sockets = [FakeSocket()]
    connect_args = {}

    def connect(url, header, timeout, sslopt):
        connect_args.update(url=url, sslopt=sslopt, header=header)
        return sockets[0]

    instance_id = uuid.uuid4()
    agent = start_opamp_client(
        "wss://opamp-server:4320/v1/opamp",
        instance_id.bytes,
        {"service.name": "cart", "service.instance.id": str(instance_id), "host.name": "h"},
        "true",
        transport=WebSocketTransport(connect=connect),
    )
    try:
        assert sockets[0].received.wait(5)
    finally:
        agent.stop(timeout=5)
    assert connect_args["url"] == "wss://opamp-server:4320/v1/opamp"
    assert connect_args["sslopt"]["check_hostname"] is False
    first = sockets[0].sent[0]
    assert first[0] == 0
    message = opamp_pb2.AgentToServer.FromString(first[1:])
    assert message.instance_uid == instance_id.bytes
    assert message.capabilities == CAPABILITIES
    assert message.health.healthy
    assert message.health.start_time_unix_nano > 0
    identifying = {kv.key: kv.value.string_value for kv in message.agent_description.identifying_attributes}
    assert identifying == {"service.name": "cart", "service.instance.id": str(instance_id)}
    last = opamp_pb2.AgentToServer.FromString(sockets[0].sent[-1][1:])
    assert last.HasField("agent_disconnect")


def test_invalid_skip_verify_is_rejected():
    with pytest.raises(ValueError, match="OPAMP_SERVER_TLS_INSECURE_SKIP_VERIFY"):
        start_opamp_client("wss://x/v1/opamp", os.urandom(16), {}, "maybe")
