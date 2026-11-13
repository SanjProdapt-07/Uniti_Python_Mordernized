# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
import os
import threading

from opentelemetry._opamp.proto import opamp_pb2
from opentelemetry.sdk.resources import OTELResourceDetector

from product_catalog.opamp import WebSocketTransport, start_opamp_client
from product_catalog.telemetry import prepare_opamp_identity

ENDPOINT_ENV = "OPAMP_SERVER_ENDPOINT"
RESOURCE_ENV = "OTEL_RESOURCE_ATTRIBUTES"


def test_prepare_opamp_identity_disabled(monkeypatch):
    monkeypatch.setenv(ENDPOINT_ENV, "")
    monkeypatch.setenv(RESOURCE_ENV, "service.namespace=opentelemetry-demo")
    assert prepare_opamp_identity() is None
    assert "service.instance.id=" not in os.environ.get(RESOURCE_ENV, "")


def test_prepare_opamp_identity_shared_with_resource(monkeypatch):
    monkeypatch.setenv(ENDPOINT_ENV, "wss://localhost:4320/v1/opamp")
    monkeypatch.setenv(RESOURCE_ENV, "service.namespace=opentelemetry-demo")
    identity = prepare_opamp_identity()
    assert identity is not None and len(identity) == 16
    assert f"service.instance.id={identity.hex()}" in os.environ.get(RESOURCE_ENV, "")
    assert OTELResourceDetector().detect().attributes["service.instance.id"] == identity.hex()


def test_prepare_opamp_identity_preserves_configured_service_instance_id(monkeypatch):
    configured = "service.namespace=opentelemetry-demo,service.instance.id=configured-id"
    monkeypatch.setenv(ENDPOINT_ENV, "wss://localhost:4320/v1/opamp")
    monkeypatch.setenv(RESOURCE_ENV, configured)
    assert prepare_opamp_identity() is not None
    assert os.environ.get(RESOURCE_ENV, "") == configured
    assert OTELResourceDetector().detect().attributes["service.instance.id"] == "configured-id"


class FakeSocket:
    connected = True

    def __init__(self):
        self.sent = []
        self.event = threading.Event()

    def send(self, data, opcode):
        self.sent.append(data)
        self.event.set()

    def recv(self):
        return b"\x00" + opamp_pb2.ServerToAgent().SerializeToString()

    def close(self):
        self.connected = False


def test_opamp_client_reports_identity_and_health():
    sock = FakeSocket()
    uid = bytes(range(1, 17))
    agent = start_opamp_client(
        "wss://opamp-server:4320/v1/opamp", uid,
        {"service.name": "product-catalog", "service.instance.id": uid.hex(), "telemetry.sdk.language": "python"},
        "", transport=WebSocketTransport(connect=lambda *a, **k: sock),
    )
    try:
        assert sock.event.wait(5)
    finally:
        agent.stop(timeout=5)
    message = opamp_pb2.AgentToServer.FromString(sock.sent[0][1:])
    assert message.instance_uid == uid
    assert message.health.healthy
    assert {kv.key for kv in message.agent_description.identifying_attributes} == {
        "service.name", "service.instance.id"}
    assert [kv.key for kv in message.agent_description.non_identifying_attributes] == ["telemetry.sdk.language"]

