# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
import logging
import ssl
import threading
import time
from urllib.parse import urlparse, urlunparse

import websocket
from opentelemetry._opamp import OpAMPAgent, OpAMPCallbacks, OpAMPClient, messages
from opentelemetry._opamp.proto import opamp_pb2
from opentelemetry._opamp.transport.base import HttpTransport

OPAMP_SERVER_ENDPOINT_ENV = "OPAMP_SERVER_ENDPOINT"
OPAMP_SERVER_TLS_INSECURE_SKIP_VERIFY_ENV = "OPAMP_SERVER_TLS_INSECURE_SKIP_VERIFY"

IDENTIFYING_KEYS = frozenset({"service.name", "service.instance.id", "service.namespace"})

CAPABILITIES = (
    opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsStatus
    | opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsHeartbeat
    | opamp_pb2.AgentCapabilities.AgentCapabilities_ReportsHealth
)

_TRUE = {"1", "t", "T", "TRUE", "true", "True"}
_FALSE = {"0", "f", "F", "FALSE", "false", "False"}


def parse_bool(value: str) -> bool:
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError(f"invalid syntax: {value!r}")


def split_attributes(attributes) -> tuple[dict, dict]:
    identifying, non_identifying = {}, {}
    for key, value in attributes.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            value = "[" + " ".join(str(v) for v in value) + "]"
        target = identifying if key in IDENTIFYING_KEYS else non_identifying
        target[key] = str(value)
    return identifying, non_identifying


class HealthReportingOpAMPClient(OpAMPClient):
    def __init__(self, **kwargs):
        super().__init__(capabilities=CAPABILITIES, **kwargs)
        self._start_time_unix_nano = time.time_ns()

    def build_full_state_message(self):
        message = super().build_full_state_message()
        message.health.CopyFrom(
            opamp_pb2.ComponentHealth(healthy=True, start_time_unix_nano=self._start_time_unix_nano)
        )
        return message


class WebSocketTransport(HttpTransport):
    def __init__(self, connect=websocket.create_connection):
        self._connect = connect
        self._socket = None
        self._lock = threading.Lock()

    def send(self, *, url, headers, data, timeout_millis, tls_certificate,
             tls_client_certificate=None, tls_client_key=None):
        with self._lock:
            try:
                if self._socket is None or not self._socket.connected:
                    self._socket = self._connect(
                        websocket_url(url),
                        header={k: v for k, v in headers.items() if k.lower() != "content-type"},
                        timeout=timeout_millis / 1e3,
                        sslopt=_ssl_options(tls_certificate),
                    )
                self._socket.send(b"\x00" + data, opcode=websocket.ABNF.OPCODE_BINARY)
                response = self._socket.recv()
            except (OSError, websocket.WebSocketException):
                self._reset()
                raise
            if not isinstance(response, bytes):
                self._reset()
                raise ValueError("OpAMP server returned a non-binary WebSocket message")
            if response.startswith(b"\x00"):
                response = response[1:]
            return messages.decode_message(response)

    def _reset(self):
        sock, self._socket = self._socket, None
        if sock is not None:
            try:
                sock.close()
            except (OSError, websocket.WebSocketException):
                pass


def websocket_url(url: str) -> str:
    parsed = urlparse(url)
    scheme = {"https": "wss", "http": "ws"}.get(parsed.scheme, parsed.scheme)
    return urlunparse(parsed._replace(scheme=scheme))


def _ssl_options(tls_certificate):
    if tls_certificate is False:
        return {"cert_reqs": ssl.CERT_NONE, "check_hostname": False}
    if isinstance(tls_certificate, str):
        return {"cert_reqs": ssl.CERT_REQUIRED, "ca_certs": tls_certificate}
    return {}


def start_opamp_client(endpoint: str, instance_uid: bytes, resource_attributes, skip_verify_value: str = "",
                       transport: HttpTransport | None = None, interval: float = 30) -> OpAMPAgent:
    skip_tls_verify = False
    if skip_verify_value:
        try:
            skip_tls_verify = parse_bool(skip_verify_value)
        except ValueError as ex:
            raise ValueError(f"parse {OPAMP_SERVER_TLS_INSECURE_SKIP_VERIFY_ENV}: {ex}") from ex
    identifying, non_identifying = split_attributes(resource_attributes)
    client = HealthReportingOpAMPClient(
        endpoint=endpoint,
        agent_identifying_attributes=identifying,
        agent_non_identifying_attributes=non_identifying,
        transport=transport or WebSocketTransport(),
        tls_certificate=not skip_tls_verify,
    )
    client.update_instance_uid(instance_uid)
    agent = OpAMPAgent(interval=interval, callbacks=OpAMPCallbacks(), client=client)
    agent.start()
    return agent


def stop_opamp_client(agent: OpAMPAgent | None, logger: logging.Logger) -> None:
    if agent is None:
        return
    agent.stop(timeout=5)
    logger.info("Stopped OpAMP client")
