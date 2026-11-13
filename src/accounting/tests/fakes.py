# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import demo_pb2


def make_order(order_id="order-1", items=(("OLJCESPC7Z", 2, 101, 960000000),)):
    order = demo_pb2.OrderResult(
        order_id=order_id,
        shipping_tracking_id="track-1",
        shipping_cost=demo_pb2.Money(currency_code="USD", units=12, nanos=300000000),
        shipping_address=demo_pb2.Address(
            street_address="1600 Amphitheatre Parkway",
            city="Mountain View",
            state="CA",
            country="United States",
            zip_code="94043",
        ),
    )
    for product_id, quantity, units, nanos in items:
        order.items.append(
            demo_pb2.OrderItem(
                item=demo_pb2.CartItem(product_id=product_id, quantity=quantity),
                cost=demo_pb2.Money(currency_code="USD", units=units, nanos=nanos),
            )
        )
    return order


class FakeError:
    def __init__(self, reason, code=-1, fatal=False):
        self._reason = reason
        self._code = code
        self._fatal = fatal

    def str(self):
        return self._reason

    def code(self):
        return self._code

    def fatal(self):
        return self._fatal


class FakeMessage:
    def __init__(self, value, topic="orders", partition=0, offset=0, key=None, headers=None, error=None, timestamp=(1, 0)):
        self._value = value
        self._topic = topic
        self._partition = partition
        self._offset = offset
        self._key = key
        self._headers = headers
        self._error = error
        self._timestamp = timestamp

    def value(self):
        return self._value

    def topic(self):
        return self._topic

    def partition(self):
        return self._partition

    def offset(self):
        return self._offset

    def key(self):
        return self._key

    def headers(self):
        return self._headers

    def error(self):
        return self._error

    def timestamp(self):
        return self._timestamp


class FakeKafkaConsumer:
    """Replays scripted poll results; sets `stop` once the script is exhausted."""

    def __init__(self, script, stop=None):
        self.script = list(script)
        self.stop = stop
        self.config = None
        self.subscriptions = []
        self.unsubscribed = 0
        self.closed = False
        self.poll_timeouts = []
        self.consume_calls = []

    def subscribe(self, topics):
        self.subscriptions.append(list(topics))

    def unsubscribe(self):
        self.unsubscribed += 1

    def _next(self):
        if not self.script:
            if self.stop is not None:
                self.stop.set()
            return None
        item = self.script.pop(0)
        if callable(item):
            return item()
        return item

    def poll(self, timeout):
        self.poll_timeouts.append(timeout)
        return self._next()

    def consume(self, num_messages=1, timeout=-1):
        self.consume_calls.append((num_messages, timeout))
        item = self._next()
        if item is None:
            return []
        return item if isinstance(item, list) else [item]

    def close(self):
        self.closed = True
