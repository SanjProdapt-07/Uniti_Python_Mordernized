# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import contextlib
import os
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from typing import Any, Protocol

from opentelemetry import context, propagate, trace
from opentelemetry.propagators.textmap import Getter
from opentelemetry.trace import Link, SpanKind, Status, StatusCode

from fraud_detection.flags import FeatureFlags
from fraud_detection.log import logger

GROUP_ID = "fraud-detection"
CLIENT_ID = f"consumer-{GROUP_ID}-1"
MAX_POLL_RECORDS = 30
POLL_TIMEOUT_SECONDS = 0.1
DEAD_SLEEP_SECONDS = 1.0
TIMESTAMP_NOT_AVAILABLE = 0
RECEIVE_TELEMETRY_ENV = "OTEL_INSTRUMENTATION_MESSAGING_EXPERIMENTAL_RECEIVE_TELEMETRY_ENABLED"
EXPERIMENTAL_ATTRIBUTES_ENV = "OTEL_INSTRUMENTATION_KAFKA_EXPERIMENTAL_SPAN_ATTRIBUTES"

_tracer = trace.get_tracer("io.opentelemetry.kafka-clients-0.11")


class KafkaConsumer(Protocol):
    def subscribe(self, topics: list[str]) -> None: ...
    def unsubscribe(self) -> None: ...
    def consume(self, num_messages: int, timeout: float) -> list[Any]: ...
    def close(self) -> None: ...


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


def _env_flag(env: Mapping[str, str], name: str) -> bool:
    return env.get(name, "false").strip().lower() == "true"


def build_consumer_config(servers: str) -> dict[str, Any]:
    return {
        "group.id": GROUP_ID,
        "client.id": CLIENT_ID,
        "bootstrap.servers": servers,
        "auto.offset.reset": "earliest",
        "enable.auto.commit": True,
    }


def _default_consumer_factory(config: dict[str, Any]) -> KafkaConsumer:
    from confluent_kafka import Consumer as ConfluentConsumer

    return ConfluentConsumer(config)


class KafkaTracing:
    def __init__(self, receive_telemetry: bool, experimental_attributes: bool):
        self.receive_telemetry = receive_telemetry
        self.experimental_attributes = experimental_attributes

    def receive(self, records: list, start: int):
        if not self.receive_telemetry or not records:
            return None
        topics = {r.topic() for r in records}
        destination = next(iter(topics)) if len(topics) == 1 else None
        attributes = {"messaging.system": "kafka"}
        if destination is not None:
            attributes["messaging.destination.name"] = destination
        attributes["messaging.client_id"] = CLIENT_ID
        attributes["messaging.operation"] = "receive"
        attributes["messaging.kafka.consumer.group"] = GROUP_ID
        span = _tracer.start_span(
            f"{destination or 'unknown'} receive",
            context=context.Context(),
            kind=SpanKind.CONSUMER,
            start_time=start,
            attributes=attributes,
        )
        span.set_attribute("messaging.batch.message_count", len(records))
        span.end()
        return trace.set_span_in_context(span, context.Context())

    @contextlib.contextmanager
    def process(self, record, receive_context) -> Iterator[None]:
        producer_context = propagate.extract(record.headers(), getter=_header_getter)
        if self.receive_telemetry:
            parent = receive_context if receive_context is not None else context.get_current()
            producer_span_context = trace.get_current_span(producer_context).get_span_context()
            links = [Link(producer_span_context)] if producer_span_context.is_valid else []
        else:
            parent = producer_context
            links = []
        span = _tracer.start_span(
            f"{record.topic()} process",
            context=parent,
            kind=SpanKind.CONSUMER,
            links=links,
            attributes=self._process_attributes(record),
        )
        token = context.attach(trace.set_span_in_context(span))
        try:
            yield
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, f"{type(exc).__name__}: {exc}"))
            raise
        finally:
            context.detach(token)
            span.end()

    def _process_attributes(self, record) -> dict[str, Any]:
        value = record.value()
        attributes: dict[str, Any] = {
            "messaging.system": "kafka",
            "messaging.destination.name": record.topic(),
            "messaging.destination.partition.id": str(record.partition()),
        }
        if value is not None:
            attributes["messaging.message.body.size"] = len(value)
        attributes["messaging.client_id"] = CLIENT_ID
        attributes["messaging.operation"] = "process"
        attributes["messaging.kafka.message.offset"] = record.offset()
        key = record.key()
        if key is not None:
            attributes["messaging.kafka.message.key"] = (
                key.decode("utf-8", errors="replace") if isinstance(key, bytes) else str(key)
            )
        if value is None:
            attributes["messaging.kafka.message.tombstone"] = True
        attributes["messaging.kafka.consumer.group"] = GROUP_ID
        if self.experimental_attributes:
            timestamp_type, timestamp = record.timestamp()
            if timestamp_type != TIMESTAMP_NOT_AVAILABLE:
                attributes["kafka.record.queue_time_ms"] = max(0, int(time.time() * 1000) - timestamp)
        return attributes


class FraudDetectionConsumer:
    def __init__(
        self,
        servers: str,
        env: Mapping[str, str] | None = None,
        consumer_factory: Callable[[dict[str, Any]], KafkaConsumer] = _default_consumer_factory,
        flags: FeatureFlags | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        env = os.environ if env is None else env
        self.topic = env.get("KAFKA_TOPIC", "orders")
        self._flags = flags or FeatureFlags()
        self._sleep = sleep
        self._tracing = KafkaTracing(
            _env_flag(env, RECEIVE_TELEMETRY_ENV),
            _env_flag(env, EXPERIMENTAL_ATTRIBUTES_ENV),
        )
        self._consumer = consumer_factory(build_consumer_config(servers))
        self._consumer.subscribe([self.topic])
        self.total_count = 0

    def run(self, stop: threading.Event) -> None:
        subscribed = True
        try:
            while not stop.is_set():
                if self._flags.get_boolean_feature_flag_value("kafkaConsumerDead"):
                    if subscribed:
                        logger.info(
                            f"FeatureFlag 'kafkaConsumerDead' is enabled, unsubscribing from '{self.topic}' "
                            "(group goes Empty, members -> 0)"
                        )
                        self._consumer.unsubscribe()
                        subscribed = False
                    stop.wait(DEAD_SLEEP_SECONDS)
                    continue
                elif not subscribed:
                    logger.info(
                        f"FeatureFlag 'kafkaConsumerDead' is disabled, re-subscribing to '{self.topic}' "
                        "(rejoining group)"
                    )
                    self._consumer.subscribe([self.topic])
                    subscribed = True

                start, records = self._poll()
                receive_context = self._tracing.receive(records, start)
                for record in records:
                    with self._tracing.process(record, receive_context):
                        self._handle(record)
        finally:
            self._consumer.close()

    def _handle(self, record) -> None:
        from demo_pb2 import OrderResult

        new_count = self.total_count + 1
        sleep_ms = self._flags.get_feature_flag_value("kafkaQueueProblems")
        if sleep_ms > 0:
            logger.info(f"FeatureFlag 'kafkaQueueProblems' is enabled, sleeping {sleep_ms}ms")
            self._sleep(sleep_ms / 1000)
        orders = OrderResult.FromString(record.value())
        logger.info(
            f"Consumed record with orderId: {orders.order_id}, and updated total count to: {new_count}"
        )
        self.total_count = new_count

    def _poll(self) -> tuple[int, list]:
        from confluent_kafka import KafkaException

        start = time.time_ns()
        messages = self._consumer.consume(num_messages=MAX_POLL_RECORDS, timeout=POLL_TIMEOUT_SECONDS)
        records = []
        for message in messages:
            error = message.error()
            if error is None:
                records.append(message)
            elif error.fatal():
                raise KafkaException(error)
        return start, records
