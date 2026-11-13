#!/usr/bin/python
# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import json
import queue
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from flagd_ui.pubsub import PubSub
from flagd_ui.scheduler import Scheduler
from flagd_ui.storage import Storage
from flagd_ui.web import SESSION_COOKIE, create_app

FIXTURE = Path(__file__).resolve().parents[2] / "flagd" / "demo.flagd.json"


@pytest.fixture
def flag_file(tmp_path: Path) -> Path:
    target = tmp_path / "demo.flagd.json"
    shutil.copyfile(FIXTURE, target)
    return target


@pytest.fixture
def pubsub() -> PubSub:
    return PubSub()


@pytest.fixture
def storage(flag_file: Path, pubsub: PubSub) -> Storage:
    return Storage(str(flag_file), pubsub)


@pytest.fixture
def make_scheduler(storage: Storage, pubsub: PubSub):
    started: list[Scheduler] = []

    def factory() -> Scheduler:
        scheduler = Scheduler(storage, pubsub)
        started.append(scheduler)
        return scheduler

    yield factory
    for scheduler in started:
        scheduler.shutdown()


class Inbox:
    def __init__(self, pubsub: PubSub, topic: str) -> None:
        self.messages: queue.Queue = queue.Queue()
        pubsub.subscribe(topic, self.messages.put)

    def receive(self, predicate, timeout: float = 2.0):
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("expected message was not received")
            try:
                message = self.messages.get(timeout=remaining)
            except queue.Empty:
                continue
            if predicate(message):
                return message


@pytest.fixture
def inbox(pubsub: PubSub):
    return lambda topic: Inbox(pubsub, topic)


def eventually(check, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if check():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def variant_of(storage: Storage, flag: str):
    data = storage.read()["flags"][flag]
    rule = (data.get("targeting") or {}).get("if")
    if isinstance(rule, list) and len(rule) == 3:
        return rule[1]
    return data.get("defaultVariant")


@pytest.fixture
def app(flag_file: Path):
    return create_app(str(flag_file))


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


class LiveSession:
    def __init__(self, socket) -> None:
        self.socket = socket

    def push(self, event: str, **values) -> None:
        self.socket.send_text(json.dumps({"__event": event, **values, "HEADERS": {"HX-Request": "true"}}))

    def receive_until(self, *needles: str, limit: int = 20) -> str:
        for _ in range(limit):
            html = self.socket.receive_text()
            if all(needle in html for needle in needles):
                return html
        raise AssertionError(f"never received {needles}")


@pytest.fixture
def live(client):
    def connect(path: str, view: str):
        response = client.get(path)
        assert response.status_code == 200
        token = client.cookies.get(SESSION_COOKIE)
        return client.websocket_connect(f"/feature/live?view={view}&_csrf_token={token}")

    return connect
