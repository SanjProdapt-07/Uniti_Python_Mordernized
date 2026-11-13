# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import itertools
import os
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from opentelemetry import context, propagate, trace
from opentelemetry.propagators.textmap import Getter
from opentelemetry.trace import Link, SpanKind, Status, StatusCode

from accounting import log
from accounting.repository import DuplicateOrderError, OrderRepository, PostgresOrderRepository, build_rows

GROUP_ID = "accounting"
POLL_TIMEOUT_SECONDS = 0.1
MAX_MESSAGE_KEY_LENGTH = 256
UNSET_PARTITION = -1
UNSET_OFFSET = -1001

_tracer = trace.get_tracer("Accounting.Consumer")
_kafka_tracer = trace.get_tracer("OpenTelemetry.AutoInstrumentation.Kafka")
_client_sequence = itertools.count(1)


class KafkaConsumer(Protocol):
    def subscribe(self, topics: list[str]) -> None: ...
    def poll(self, timeout: float) -> Any: ...
    def close(self) -> None: ...


class ConsumeError(Exception):
    def __init__(self, error):
        self.error = error
        self.reason = error.str() if hasattr(error, "str") else str(error)
        super().__init__(self.reason)


class _Cancelled(Exception):
    pass


class _HeaderGetter(Getter):
    def get(self, carrier, key):
        values = [v for k, v in carrier or [] if k == key and v is not None]
        if not values:
            return None
        value = values[-1]
        return [value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value]

    def keys(self, carrier):
        return [k for k, _ in carrier or []]


_header_getter = _HeaderGetter()


def build_consumer_config(servers: str) -> dict[str, Any]:
    return {
        "group.id": GROUP_ID,
        "bootstrap.servers": servers,
        "auto.offset.reset": "earliest",
        "enable.auto.commit": True,
    }


def _default_consumer_factory(config: dict[str, Any]) -> KafkaConsumer:
    from confluent_kafka import Consumer as ConfluentConsumer

    return ConfluentConsumer(config)


class Consumer:
    def __init__(
        self,
        env: Mapping[str, str] | None = None,
        consumer_factory: Callable[[dict[str, Any]], KafkaConsumer] = _default_consumer_factory,
        repository_factory: Callable[[str], OrderRepository] = PostgresOrderRepository,
    ):
        env = os.environ if env is None else env
        self.topic = env.get("KAFKA_TOPIC", "orders")
        servers = env.get("KAFKA_ADDR")
        if servers is None:
            raise RuntimeError("The KAFKA_ADDR environment variable is not set.")

        config = build_consumer_config(servers)
        self._client_name = f"{config.get('client.id', 'rdkafka')}#consumer-{next(_client_sequence)}"
        self._consumer = consumer_factory(config)
        self._consumer.subscribe([self.topic])

        log.kafka_connecting(servers)

        connection_string = env.get("DB_CONNECTION_STRING")
        self._repository = repository_factory(connection_string) if connection_string is not None else None

    def run(self, stop: threading.Event) -> None:
        try:
            while not stop.is_set():
                try:
                    with _tracer.start_as_current_span(
                        "order-consumed",
                        context=context.Context(),
                        kind=SpanKind.INTERNAL,
                        record_exception=False,
                        set_status_on_exception=False,
                    ):
                        message = self._consume(stop)
                        self.process_message(message)
                except ConsumeError as e:
                    log.consume_error(e, e.reason)
        except _Cancelled:
            pass
        finally:
            log.consumer_closing()
            self._consumer.close()

    def _consume(self, stop: threading.Event):
        while True:
            if stop.is_set():
                raise _Cancelled()
            start = time.time_ns()
            message = self._consumer.poll(POLL_TIMEOUT_SECONDS)
            if message is None:
                continue
            error = message.error()
            self._record_receive_span(message, start, error)
            if error is not None:
                raise ConsumeError(error)
            return message

    def _record_receive_span(self, message, start: int, error) -> None:
        topic = message.topic()
        if error is not None and topic is None:
            return
        carrier = message.headers()
        span_context = trace.get_current_span(propagate.extract(carrier, getter=_header_getter)).get_span_context()
        links = [Link(span_context)] if span_context.is_valid else []
        span = _kafka_tracer.start_span(
            f"{topic} receive" if topic else "receive",
            kind=SpanKind.CONSUMER,
            start_time=start,
            links=links,
            attributes={"messaging.client_id": self._client_name, "messaging.kafka.consumer.group": GROUP_ID},
        )
        span.set_attribute("messaging.operation", "receive")
        span.set_attribute("messaging.system", "kafka")
        if topic:
            span.set_attribute("messaging.destination.name", topic)
        key = message.key()
        if key is not None:
            if isinstance(key, bytes):
                key = key.decode("utf-8", errors="replace")
            span.set_attribute("messaging.kafka.message.key", key[:MAX_MESSAGE_KEY_LENGTH])
        partition = message.partition()
        offset = message.offset()
        span.set_attribute("messaging.kafka.destination.partition", UNSET_PARTITION if partition is None else partition)
        span.set_attribute("messaging.kafka.message.offset", UNSET_OFFSET if offset is None else offset)
        if error is not None:
            exc = ConsumeError(error)
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, exc.reason))
        span.end()

    def process_message(self, message) -> None:
        from demo_pb2 import OrderResult

        try:
            order = OrderResult.FromString(message.value())
            log.order_received_message(order)

            if self._repository is None:
                return

            self._repository.save(build_rows(order))
        except DuplicateOrderError:
            log.duplicate_order_skipped()
        except Exception as ex:
            log.order_parsing_failed(ex)
