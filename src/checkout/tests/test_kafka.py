# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import threading

import demo_pb2
from confluent_kafka import Producer

from checkout import kafka
from checkout.service import CheckoutService


class _Ctx:
    def remaining(self):
        return 5.0

    def err(self):
        return None


def test_topic_default():
    assert kafka.TOPIC == "orders"


def test_real_producer_against_librdkafka_mock_cluster():
    producer = kafka.KafkaProducer(
        Producer({"test.mock.num.brokers": 1, "acks": 0, "linger.ms": 0, "message.timeout.ms": 5000})
    )
    try:
        svc = CheckoutService(
            shipping_addr="", email_addr="", cart=None, product_catalog=None, currency=None, payment=None,
            http_client=None, kafka_broker_addr="mock", producer=producer,
        )
        reports = []
        done = threading.Event()

        def on_delivery(err, msg):
            reports.append((err, msg.offset(), msg.headers(), msg.key()))
            done.set()

        producer.produce("orders", b"x", [("traceparent", b"00-abc")], on_delivery)
        assert done.wait(10)
        err, offset, headers, key = reports[0]
        assert err is None
        assert key is None

        svc.send_to_post_processor(_Ctx(), demo_pb2.OrderResult(order_id="o-1"))
    finally:
        producer.close()


def test_create_kafka_producer_config():
    import logging

    p = kafka.create_kafka_producer(["127.0.0.1:1"], logging.getLogger("checkout"))
    p.close(0)
