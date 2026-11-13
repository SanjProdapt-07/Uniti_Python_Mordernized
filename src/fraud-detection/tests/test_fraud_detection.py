# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import io
import logging
import re
import threading

import pytest
from opentelemetry import trace
from opentelemetry.trace import SpanKind, StatusCode

from fakes import FakeError, FakeKafkaConsumer, FakeMessage, make_order
from fraud_detection import log
from fraud_detection.consumer import FraudDetectionConsumer, build_consumer_config
from fraud_detection.flags import FeatureFlags

ENV = {
    "KAFKA_TOPIC": "orders",
    "OTEL_INSTRUMENTATION_MESSAGING_EXPERIMENTAL_RECEIVE_TELEMETRY_ENABLED": "true",
    "OTEL_INSTRUMENTATION_KAFKA_EXPERIMENTAL_SPAN_ATTRIBUTES": "true",
}


class FakeFlagClient:
    def __init__(self, ints=None, bools=None):
        self.ints = ints or {}
        self.bools = bools or {}
        self.calls = []

    def get_integer_value(self, key, default, ctx=None):
        self.calls.append((key, default, ctx, trace.get_current_span().get_span_context()))
        value = self.ints.get(key, default)
        return value() if callable(value) else value

    def get_boolean_value(self, key, default, ctx=None):
        self.calls.append((key, default, ctx, trace.get_current_span().get_span_context()))
        value = self.bools.get(key, default)
        return value() if callable(value) else value


def make(script, env=ENV, flag_client=None):
    stop = threading.Event()
    kafka = FakeKafkaConsumer(script, stop)
    sleeps = []

    def factory(config):
        kafka.config = config
        return kafka

    client = flag_client or FakeFlagClient()
    consumer = FraudDetectionConsumer(
        "kafka:9092", env=env, consumer_factory=factory, flags=FeatureFlags(client), sleep=sleeps.append
    )
    return consumer, kafka, client, sleeps, stop


def record(order_id, offset, headers=None):
    return FakeMessage(make_order(order_id).SerializeToString(), offset=offset, headers=headers, timestamp=(1, 1000))


def test_consumer_config():
    assert build_consumer_config("kafka:9092") == {
        "group.id": "fraud-detection",
        "client.id": "consumer-fraud-detection-1",
        "bootstrap.servers": "kafka:9092",
        "auto.offset.reset": "earliest",
        "enable.auto.commit": True,
    }


def test_consumes_batches_counts_and_logs(caplog, spans, producer_context):
    caplog.set_level(logging.INFO)
    producer_ctx, headers = producer_context
    batch = [record("o-1", 5, headers), record("o-2", 6, headers)]
    consumer, kafka, client, sleeps, stop = make([[], batch, [record("o-3", 7)]])
    consumer.run(stop)

    assert kafka.subscriptions == [["orders"]]
    assert kafka.consume_calls[0] == (30, 0.1)
    assert kafka.closed
    assert consumer.total_count == 3
    messages = [r.getMessage() for r in caplog.records if r.name == "fraud-detection"]
    assert messages == [
        "Consumed record with orderId: o-1, and updated total count to: 1",
        "Consumed record with orderId: o-2, and updated total count to: 2",
        "Consumed record with orderId: o-3, and updated total count to: 3",
    ]
    assert sleeps == []

    finished = spans.get_finished_spans()
    receives = [s for s in finished if s.name == "orders receive"]
    processes = [s for s in finished if s.name == "orders process"]
    assert len(receives) == 2 and len(processes) == 3
    first_receive = receives[0]
    assert first_receive.kind == SpanKind.CONSUMER and first_receive.parent is None
    assert first_receive.instrumentation_scope.name == "io.opentelemetry.kafka-clients-0.11"
    assert dict(first_receive.attributes) == {
        "messaging.system": "kafka",
        "messaging.destination.name": "orders",
        "messaging.client_id": "consumer-fraud-detection-1",
        "messaging.operation": "receive",
        "messaging.kafka.consumer.group": "fraud-detection",
        "messaging.batch.message_count": 2,
    }
    for process in processes[:2]:
        assert process.parent.span_id == first_receive.context.span_id
        assert [link.context.span_id for link in process.links] == [producer_ctx.span_id]
    assert processes[2].links == ()
    attrs = dict(processes[0].attributes)
    queue_time = attrs.pop("kafka.record.queue_time_ms")
    assert queue_time > 0
    assert attrs == {
        "messaging.system": "kafka",
        "messaging.destination.name": "orders",
        "messaging.destination.partition.id": "0",
        "messaging.message.body.size": len(batch[0].value()),
        "messaging.client_id": "consumer-fraud-detection-1",
        "messaging.operation": "process",
        "messaging.kafka.message.offset": 5,
        "messaging.kafka.consumer.group": "fraud-detection",
    }

    queue_calls = [c for c in client.calls if c[0] == "kafkaQueueProblems"]
    assert len(queue_calls) == 3
    assert all(c[1] == 0 for c in queue_calls)
    assert queue_calls[0][3].span_id == processes[0].context.span_id
    sessions = {c[2].attributes["session"] for c in client.calls}
    assert len(sessions) == len(client.calls)
    assert all(c[2].targeting_key is None for c in client.calls)
    dead_calls = [c for c in client.calls if c[0] == "kafkaConsumerDead"]
    assert dead_calls and all(c[1] is False and not c[3].is_valid for c in dead_calls)


def test_without_receive_telemetry_process_is_child_of_producer(spans, producer_context):
    producer_ctx, headers = producer_context
    consumer, _, _, _, stop = make([[record("o-1", 1, headers)]], env={})
    consumer.run(stop)
    finished = spans.get_finished_spans()
    assert [s.name for s in finished] == ["orders process"]
    assert finished[0].parent.span_id == producer_ctx.span_id
    assert finished[0].links == ()
    assert "kafka.record.queue_time_ms" not in finished[0].attributes


def test_kafka_queue_problems_sleeps_per_record(caplog):
    caplog.set_level(logging.INFO)
    client = FakeFlagClient(ints={"kafkaQueueProblems": 3000})
    consumer, _, _, sleeps, stop = make([[record("o-1", 1), record("o-2", 2)]], flag_client=client)
    consumer.run(stop)
    assert sleeps == [3.0, 3.0]
    msgs = [r.getMessage() for r in caplog.records]
    assert msgs[0] == "FeatureFlag 'kafkaQueueProblems' is enabled, sleeping 3000ms"
    assert msgs[1] == "Consumed record with orderId: o-1, and updated total count to: 1"


def test_kafka_consumer_dead_unsubscribes_and_resubscribes(caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    states = iter([True, True, False])
    client = FakeFlagClient(bools={"kafkaConsumerDead": lambda: next(states, False)})
    consumer, kafka, _, _, stop = make([[record("o-1", 1)]], flag_client=client)
    waits = []
    monkeypatch.setattr(stop, "wait", lambda t: waits.append(t))
    consumer.run(stop)
    assert kafka.unsubscribed == 1
    assert kafka.subscriptions == [["orders"], ["orders"]]
    assert waits == [1.0, 1.0]
    assert len(kafka.consume_calls) == 2
    msgs = [r.getMessage() for r in caplog.records]
    assert msgs[:2] == [
        "FeatureFlag 'kafkaConsumerDead' is enabled, unsubscribing from 'orders' (group goes Empty, members -> 0)",
        "FeatureFlag 'kafkaConsumerDead' is disabled, re-subscribing to 'orders' (rejoining group)",
    ]


def test_non_fatal_kafka_errors_are_skipped():
    err = FakeMessage(None, error=FakeError("Broker: Unknown topic or partition"))
    consumer, _, _, _, stop = make([[err, record("o-1", 1)]])
    consumer.run(stop)
    assert consumer.total_count == 1


def test_fatal_kafka_error_raises_and_closes():
    from confluent_kafka import KafkaException

    consumer, kafka, _, _, stop = make([[FakeMessage(None, error=FakeError("fatal", fatal=True))]])
    with pytest.raises(KafkaException):
        consumer.run(stop)
    assert kafka.closed


def test_unparseable_record_stops_consumer(spans):
    consumer, kafka, _, _, stop = make([[FakeMessage(b"\xff\xff\xff")]])
    with pytest.raises(Exception):
        consumer.run(stop)
    assert kafka.closed
    process = next(s for s in spans.get_finished_spans() if s.name == "orders process")
    assert process.status.status_code == StatusCode.ERROR


def test_log_pattern_includes_trace_context(caplog):
    caplog.set_level(logging.INFO)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(log.PatternFormatter())
    log.logger.addHandler(handler)
    try:
        log.logger.info("outside")
        with trace.get_tracer("t").start_as_current_span("s") as span:
            log.logger.info("inside")
    finally:
        log.logger.removeHandler(handler)
    outside, inside = stream.getvalue().splitlines()
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d - fraud-detection - outside trace_id= span_id= trace_flags= ", outside)
    ctx = span.get_span_context()
    assert inside.endswith(f"- inside trace_id={ctx.trace_id:032x} span_id={ctx.span_id:016x} trace_flags=01 ")


def test_missing_kafka_addr_exits(monkeypatch, capsys):
    from fraud_detection import __main__ as entry

    monkeypatch.delenv("KAFKA_ADDR", raising=False)
    monkeypatch.setattr(entry.flags, "init_provider", lambda: None)
    monkeypatch.setattr(entry.log, "configure_console", lambda: None)
    with pytest.raises(SystemExit) as exc:
        entry.main()
    assert exc.value.code == 1
    assert capsys.readouterr().out == "KAFKA_ADDR is not supplied\n"


def test_flags_use_default_client_with_defaults():
    from openfeature import api
    from openfeature.provider.no_op_provider import NoOpProvider

    api.set_provider(NoOpProvider())
    flags = FeatureFlags()
    assert flags.get_feature_flag_value("kafkaQueueProblems") == 0
    assert flags.get_boolean_feature_flag_value("kafkaConsumerDead") is False
