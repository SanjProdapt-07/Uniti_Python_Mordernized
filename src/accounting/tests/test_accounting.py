# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import contextlib
import io
import logging
import threading

import pytest
from opentelemetry.trace import SpanKind, StatusCode

from accounting import helpers, log
from accounting.consumer import Consumer, build_consumer_config
from accounting.repository import (
    ORDER_ITEM_SQL,
    ORDER_SQL,
    SHIPPING_SQL,
    DuplicateOrderError,
    PostgresOrderRepository,
    build_rows,
    parse_connection_string,
)
from fakes import FakeError, FakeKafkaConsumer, FakeMessage, make_order

ENV = {
    "KAFKA_ADDR": "kafka:9092",
    "KAFKA_TOPIC": "orders",
    "DB_CONNECTION_STRING": "Host=astronomy-db;Username=astronomy_user;Password=astronomy_password;Database=astronomy_db",
}


class FakeRepository:
    def __init__(self, connection_string, error=None):
        self.connection_string = connection_string
        self.saved = []
        self.error = error

    def save(self, rows):
        if self.error is not None:
            raise self.error
        self.saved.append(rows)


def make_consumer(script, env=ENV, repository_error=None):
    stop = threading.Event()
    kafka = FakeKafkaConsumer(script, stop)
    repos = []

    def consumer_factory(config):
        kafka.config = config
        return kafka

    def repository_factory(conn):
        repo = FakeRepository(conn, repository_error)
        repos.append(repo)
        return repo

    consumer = Consumer(env=env, consumer_factory=consumer_factory, repository_factory=repository_factory)
    return consumer, kafka, repos[0] if repos else None, stop


def test_consumer_config_and_subscription(caplog):
    caplog.set_level(logging.INFO)
    consumer, kafka, repo, _ = make_consumer([])
    assert kafka.config == {
        "group.id": "accounting",
        "bootstrap.servers": "kafka:9092",
        "auto.offset.reset": "earliest",
        "enable.auto.commit": True,
    }
    assert kafka.subscriptions == [["orders"]]
    assert repo.connection_string == ENV["DB_CONNECTION_STRING"]
    record = caplog.records[-1]
    assert record.name == "Accounting.Consumer"
    assert record.getMessage() == "Connecting to Kafka: {servers}"
    assert log.render(record) == "Connecting to Kafka: kafka:9092"
    assert getattr(record, "otel.event.name") == "accounting.kafka.connecting"


def test_topic_defaults_to_orders():
    _, kafka, _, _ = make_consumer([], env={"KAFKA_ADDR": "k:1"})
    assert kafka.subscriptions == [["orders"]]


def test_missing_kafka_addr_raises():
    with pytest.raises(RuntimeError, match="The KAFKA_ADDR environment variable is not set."):
        Consumer(env={}, consumer_factory=lambda c: FakeKafkaConsumer([]))


def test_message_persisted_with_exact_rows_and_spans(spans, producer_context, caplog):
    caplog.set_level(logging.INFO)
    producer_ctx, headers = producer_context
    spans.clear()
    order = make_order(items=[("OLJCESPC7Z", 2, 101, 960000000), ("66VCHSJNUP", 1, 349, 950000000)])
    msg = FakeMessage(order.SerializeToString(), partition=0, offset=42, headers=headers)
    consumer, kafka, repo, stop = make_consumer([None, msg])

    consumer.run(stop)

    assert kafka.closed
    assert kafka.poll_timeouts[0] == 0.1
    assert len(repo.saved) == 1
    rows = repo.saved[0]
    assert rows.statements() == [
        (ORDER_SQL, ("order-1",)),
        (ORDER_ITEM_SQL, ("USD", 349, 950000000, "66VCHSJNUP", 1, "order-1")),
        (ORDER_ITEM_SQL, ("USD", 101, 960000000, "OLJCESPC7Z", 2, "order-1")),
        (
            SHIPPING_SQL,
            ("track-1", "USD", 12, 300000000, "1600 Amphitheatre Parkway", "Mountain View", "CA", "United States", "94043", "order-1"),
        ),
    ]

    finished = spans.get_finished_spans()
    receive = [s for s in finished if s.name == "orders receive"]
    consumed = [s for s in finished if s.name == "order-consumed"]
    assert len(receive) == 1
    receive = receive[0]
    parent = next(s for s in consumed if s.context.span_id == receive.parent.span_id)
    assert parent.kind == SpanKind.INTERNAL and parent.parent is None
    assert parent.instrumentation_scope.name == "Accounting.Consumer"
    assert receive.kind == SpanKind.CONSUMER
    assert receive.instrumentation_scope.name == "OpenTelemetry.AutoInstrumentation.Kafka"
    assert [link.context.span_id for link in receive.links] == [producer_ctx.span_id]
    assert receive.context.trace_id != producer_ctx.trace_id
    assert dict(receive.attributes) == {
        "messaging.client_id": receive.attributes["messaging.client_id"],
        "messaging.kafka.consumer.group": "accounting",
        "messaging.operation": "receive",
        "messaging.system": "kafka",
        "messaging.destination.name": "orders",
        "messaging.kafka.destination.partition": 0,
        "messaging.kafka.message.offset": 42,
    }
    assert receive.attributes["messaging.client_id"].startswith("rdkafka#consumer-")

    messages = [(r.name, log.render(r)) for r in caplog.records]
    assert ("Accounting.Consumer", "Closing consumer") == messages[-1]
    received = next(r for r in caplog.records if getattr(r, "otel.event.name", None) == "accounting.order.received")
    assert received.getMessage() == "Order details: {@OrderResult}."
    assert log.render(received).startswith('Order details: { "orderId": "order-1", "shippingTrackingId": "track-1", "shippingCost": { "currencyCode": "USD", "units": "12", "nanos": 300000000 }')


def test_no_db_connection_string_only_logs(caplog):
    caplog.set_level(logging.INFO)
    env = {"KAFKA_ADDR": "kafka:9092"}
    msg = FakeMessage(make_order().SerializeToString())
    consumer, kafka, repo, stop = make_consumer([msg], env=env)
    assert repo is None
    consumer.run(stop)
    events = [getattr(r, "otel.event.name", None) for r in caplog.records]
    assert "accounting.order.received" in events
    assert "accounting.order.parsing_failed" not in events


def test_duplicate_order_is_skipped(caplog):
    caplog.set_level(logging.INFO)
    msg = FakeMessage(make_order().SerializeToString())
    consumer, _, _, stop = make_consumer([msg], repository_error=DuplicateOrderError("dup"))
    consumer.run(stop)
    rec = next(r for r in caplog.records if getattr(r, "otel.event.name", None) == "accounting.order.duplicate_skipped")
    assert rec.levelno == logging.INFO and rec.getMessage() == "Duplicate order received, skipping."


def test_invalid_payload_logs_parsing_failed(caplog):
    caplog.set_level(logging.INFO)
    consumer, _, repo, stop = make_consumer([FakeMessage(b"\xff\xff\xff"), FakeMessage(None)])
    consumer.run(stop)
    failed = [r for r in caplog.records if getattr(r, "otel.event.name", None) == "accounting.order.parsing_failed"]
    assert len(failed) == 2
    assert all(r.levelno == logging.ERROR and r.exc_info and r.getMessage() == "Order parsing failed:" for r in failed)
    assert repo.saved == []


def test_db_failure_logs_parsing_failed(caplog):
    caplog.set_level(logging.INFO)
    consumer, _, _, stop = make_consumer([FakeMessage(make_order().SerializeToString())], repository_error=OSError("down"))
    consumer.run(stop)
    assert any(getattr(r, "otel.event.name", None) == "accounting.order.parsing_failed" for r in caplog.records)


def test_missing_shipping_cost_writes_nothing(caplog):
    caplog.set_level(logging.INFO)
    order = make_order()
    order.ClearField("shipping_cost")
    consumer, _, repo, stop = make_consumer([FakeMessage(order.SerializeToString())])
    consumer.run(stop)
    assert repo.saved == []
    assert any(getattr(r, "otel.event.name", None) == "accounting.order.parsing_failed" for r in caplog.records)


def test_duplicate_product_in_order_is_rejected_before_db():
    with pytest.raises(ValueError, match="already being tracked"):
        build_rows(make_order(items=[("A", 1, 1, 0), ("A", 2, 1, 0)]))


def test_consume_error_logged_and_span_marked(spans, caplog):
    caplog.set_level(logging.INFO)
    spans.clear()
    err = FakeMessage(None, offset=7, error=FakeError("Broker: Unknown topic or partition"))
    consumer, _, _, stop = make_consumer([err])
    consumer.run(stop)
    rec = next(r for r in caplog.records if getattr(r, "otel.event.name", None) == "accounting.kafka.consume_failed")
    assert rec.levelno == logging.ERROR
    assert log.render(rec) == "Consume error: Broker: Unknown topic or partition"
    receive = next(s for s in spans.get_finished_spans() if s.name == "orders receive")
    assert receive.status.status_code == StatusCode.ERROR
    assert receive.events[0].name == "exception"
    consumed = [s for s in spans.get_finished_spans() if s.name == "order-consumed"]
    assert all(s.status.status_code == StatusCode.UNSET for s in consumed)


def test_shutdown_closes_consumer_and_logs(caplog):
    caplog.set_level(logging.INFO)
    consumer, kafka, _, stop = make_consumer([])
    stop.set()
    consumer.run(stop)
    assert kafka.closed
    assert log.render(caplog.records[-1]) == "Closing consumer"


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params):
        if self.conn.fail_with is not None:
            raise self.conn.fail_with
        self.conn.executed.append((sql, params))


class FakeInfo:
    backend_pid = 4321


class FakeConnection:
    def __init__(self, fail_with=None):
        self.executed = []
        self.events = []
        self.closed = False
        self.broken = False
        self.fail_with = fail_with
        self.info = FakeInfo()

    @contextlib.contextmanager
    def transaction(self):
        self.events.append("BEGIN")
        try:
            yield
        except BaseException:
            self.events.append("ROLLBACK")
            raise
        self.events.append("COMMIT")

    @contextlib.contextmanager
    def pipeline(self):
        yield

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


class UniqueViolation(Exception):
    sqlstate = "23505"


def test_postgres_repository_writes_in_one_transaction(spans):
    spans.clear()
    conn = FakeConnection()
    calls = []

    def connect(**kwargs):
        calls.append(kwargs)
        return conn

    repo = PostgresOrderRepository(ENV["DB_CONNECTION_STRING"], connect=connect)
    rows = build_rows(make_order())
    repo.save(rows)
    repo.save(build_rows(make_order("order-2")))

    assert calls == [
        {"autocommit": True, "host": "astronomy-db", "user": "astronomy_user", "password": "astronomy_password", "dbname": "astronomy_db"}
    ]
    assert conn.events == ["BEGIN", "COMMIT", "BEGIN", "COMMIT"]
    assert conn.executed[:3] == rows.statements()
    span = spans.get_finished_spans()[0]
    assert span.name == "postgresql" and span.kind == SpanKind.CLIENT
    assert span.instrumentation_scope.name == "Npgsql"
    assert dict(span.attributes) == {
        "db.query.text": ";\n".join(sql for sql, _ in rows.statements()),
        "db.system.name": "postgresql",
        "db.namespace": "astronomy_db",
        "server.address": "astronomy-db",
        "db.npgsql.data_source": "Host=astronomy-db;Username=astronomy_user;Database=astronomy_db",
        "db.npgsql.connection_id": 4321,
    }


def test_postgres_repository_unique_violation(spans):
    spans.clear()
    conn = FakeConnection(fail_with=UniqueViolation("duplicate key"))
    repo = PostgresOrderRepository(ENV["DB_CONNECTION_STRING"], connect=lambda **kw: conn)
    with pytest.raises(DuplicateOrderError):
        repo.save(build_rows(make_order()))
    assert conn.events == ["BEGIN", "ROLLBACK"]
    span = spans.get_finished_spans()[0]
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes["db.response.status_code"] == "23505"
    assert span.attributes["error.type"] == "23505"


def test_parse_connection_string():
    params, public = parse_connection_string("Server=db; Port=5433; User ID=u; PWD='p;'; Database=d")
    assert params == {"host": "db", "port": "5433", "user": "u", "password": "p;", "dbname": "d"}
    assert public == "Server=db;Port=5433;User ID=u;Database=d"


def test_env_output_in_order_redacts_secrets():
    env = {
        "OTEL_SERVICE_NAME": "accounting",
        "KAFKA_ADDR": "kafka:9092",
        "OTEL_EXPORTER_OTLP_HEADERS": "api-key=secret",
        "PATH": "/usr/bin",
        "otel_resource_attributes": "a=b",
    }
    out = []
    helpers.output_in_order(helpers.filter_relevant(env), write=out.append)
    assert out == [
        "[KAFKA_ADDR, kafka:9092]",
        "[OTEL_EXPORTER_OTLP_HEADERS, [REDACTED]]",
        "[otel_resource_attributes, a=b]",
        "[OTEL_SERVICE_NAME, accounting]",
    ]


def test_console_format_matches_dotnet_simple_console(caplog):
    caplog.set_level(logging.INFO)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(log.ConsoleFormatter())
    log.logger.addHandler(handler)
    try:
        log.kafka_connecting("kafka:9092")
    finally:
        log.logger.removeHandler(handler)
    assert stream.getvalue() == "info: Accounting.Consumer[0]\n      Connecting to Kafka: kafka:9092\n"


def test_build_consumer_config():
    assert build_consumer_config("a:1")["bootstrap.servers"] == "a:1"
