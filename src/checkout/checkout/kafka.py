# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import os
import threading

TOPIC = os.environ.get("KAFKA_TOPIC") or "orders"


class KafkaProducer:
    """confluent-kafka producer with a background thread serving delivery reports."""

    def __init__(self, producer):
        self._producer = producer
        self._stopped = threading.Event()
        self._poller = threading.Thread(target=self._poll, name="kafka-poll", daemon=True)
        self._poller.start()

    def _poll(self):
        while not self._stopped.is_set():
            self._producer.poll(0.1)

    def produce(self, topic, value, headers, on_delivery):
        self._producer.produce(topic, value=value, headers=headers, on_delivery=on_delivery)

    def close(self, timeout=5.0):
        self._stopped.set()
        self._poller.join(timeout=1.0)
        self._producer.flush(timeout)


def create_kafka_producer(brokers, logger):
    from confluent_kafka import Producer

    producer = Producer(
        {
            "bootstrap.servers": ",".join(brokers),
            "acks": 0,
            "linger.ms": 0,
            "message.timeout.ms": 5000,
        },
        logger=logger,
    )
    return KafkaProducer(producer)
