# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
import logging
import threading
import time
from typing import Protocol

import grpc
import redis
from opentelemetry import metrics, trace
from redis.backoff import ExponentialBackoff

import demo_pb2

logger = logging.getLogger("cart.cartstore.ValkeyCartStore")

CART_FIELD_NAME = "cart"
CART_TTL_SECONDS = 60 * 60
REDIS_RETRY_NUMBER = 30
LATENCY_BUCKETS = [0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1, 2.5, 5, 7.5, 10]

_meter = metrics.get_meter("OpenTelemetry.Demo.Cart")
add_item_histogram = _meter.create_histogram(
    "demo.cart.add_item.latency", unit="s", explicit_bucket_boundaries_advisory=LATENCY_BUCKETS
)
get_cart_histogram = _meter.create_histogram(
    "demo.cart.get_cart.latency", unit="s", explicit_bucket_boundaries_advisory=LATENCY_BUCKETS
)


def log(level: int, event_name: str, message: str, **attributes) -> None:
    logger.log(level, message, extra={"event.name": event_name, **attributes})


class CartStoreError(Exception):
    def __init__(self, code: grpc.StatusCode, details: str):
        super().__init__(details)
        self.code = code
        self.details = details


class CartStore(Protocol):
    def initialize(self) -> None: ...
    def add_item(self, user_id: str, product_id: str, quantity: int) -> None: ...
    def empty_cart(self, user_id: str) -> None: ...
    def get_cart(self, user_id: str) -> demo_pb2.Cart: ...
    def ping(self) -> bool: ...


def _storage_error(ex: Exception) -> CartStoreError:
    return CartStoreError(
        grpc.StatusCode.FAILED_PRECONDITION,
        f"Can't access cart storage. {type(ex).__module__}.{type(ex).__qualname__}: {ex}",
    )


def parse_address(address: str) -> tuple[str, int]:
    endpoint = address.split(",")[0].strip()
    host, sep, port = endpoint.rpartition(":")
    if not sep:
        return endpoint, 6379
    return host.strip("[]"), int(port)


class ValkeyCartStore:
    def __init__(
        self,
        valkey_address: str,
        feature_client,
        valkey_password: str | None = None,
        valkey_tls: bool = False,
        connect_retry: int = REDIS_RETRY_NUMBER,
        client_factory=redis.Redis,
    ):
        self._feature_client = feature_client
        self._empty_cart_bytes = demo_pb2.Cart().SerializeToString()
        self._connection_string = (
            f"{valkey_address},ssl={'true' if valkey_tls else 'false'},allowAdmin=true,abortConnect=false"
        )
        self._host, self._port = parse_address(valkey_address)
        self._password = valkey_password or None
        self._tls = valkey_tls
        self._connect_retry = connect_retry
        self._client_factory = client_factory
        self._redis = None
        self._is_redis_connection_opened = False
        self._lock = threading.Lock()

    def initialize(self) -> None:
        self._ensure_redis_connected(self._connect_retry)

    def _ensure_redis_connected(self, attempts: int = 1):
        if self._is_redis_connection_opened and self._redis is not None:
            return self._redis

        with self._lock:
            if self._is_redis_connection_opened and self._redis is not None:
                return self._redis

            log(logging.DEBUG, "cart.redis.connecting", f"Connecting to Redis: {self._connection_string}",
                connectionString=self._connection_string)

            client = self._client_factory(
                host=self._host,
                port=self._port,
                password=self._password,
                ssl=self._tls,
                socket_connect_timeout=5,
                socket_keepalive=True,
                health_check_interval=180,
            )
            backoff = ExponentialBackoff(cap=10, base=1)
            connected = False
            for attempt in range(max(attempts, 1)):
                try:
                    connected = bool(client.ping())
                    break
                except Exception:
                    if attempt + 1 < attempts:
                        time.sleep(backoff.compute(attempt + 1))

            if not connected:
                log(logging.ERROR, "cart.redis.connection_failed", "Wasn't able to connect to redis")
                raise ConnectionError("Wasn't able to connect to redis")

            log(logging.INFO, "cart.redis.connected", "Successfully connected to Redis")
            log(logging.DEBUG, "cart.redis.small_test", "Performing small test")
            client.set("cart", "OK")
            res = client.get("cart")
            res = res.decode() if isinstance(res, bytes) else res
            log(logging.DEBUG, "cart.redis.small_test_result", f"Small test result: {res}", result=str(res))

            self._redis = client
            self._is_redis_connection_opened = True
            return client

    def _connection_lost(self) -> None:
        if self._is_redis_connection_opened:
            log(logging.INFO, "cart.redis.connection_lost", "Connection failed. Disposing the object")
        self._is_redis_connection_opened = False

    def _run(self, operation):
        try:
            client = self._ensure_redis_connected()
            return operation(client)
        except redis.ConnectionError as ex:
            self._connection_lost()
            raise _storage_error(ex) from ex
        except Exception as ex:
            raise _storage_error(ex) from ex

    def add_item(self, user_id: str, product_id: str, quantity: int) -> None:
        start = time.perf_counter()
        log(logging.INFO, "cart.add_item",
            f"AddItemAsync called with userId={user_id}, productId={product_id}, quantity={quantity}",
            userId=user_id, productId=product_id, quantity=quantity)

        def operation(client):
            value = client.hget(user_id, CART_FIELD_NAME)
            if value is None:
                cart = demo_pb2.Cart(user_id=user_id)
                cart.items.add(product_id=product_id, quantity=quantity)
            else:
                cart = demo_pb2.Cart.FromString(value)
                existing = next((i for i in cart.items if i.product_id == product_id), None)
                if existing is None:
                    cart.items.add(product_id=product_id, quantity=quantity)
                else:
                    existing.quantity += quantity
            client.hset(user_id, mapping={CART_FIELD_NAME: cart.SerializeToString()})
            client.expire(user_id, CART_TTL_SECONDS)

        try:
            self._run(operation)
        finally:
            add_item_histogram.record(time.perf_counter() - start)

    def empty_cart(self, user_id: str) -> None:
        log(logging.INFO, "cart.empty", f"EmptyCartAsync called with userId={user_id}", userId=user_id)

        def operation(client):
            client.hset(user_id, mapping={CART_FIELD_NAME: self._empty_cart_bytes})
            client.expire(user_id, CART_TTL_SECONDS)

        self._run(operation)

    def get_cart(self, user_id: str) -> demo_pb2.Cart:
        overflow_enabled = self._feature_client.get_boolean_value("cartMetricCardinalityOverflow", False)
        span_context = trace.get_current_span().get_span_context()
        start = time.perf_counter()
        log(logging.INFO, "cart.get", f"GetCartAsync called with userId={user_id}", userId=user_id)

        def operation(client):
            value = client.hget(user_id, CART_FIELD_NAME)
            if value is not None:
                return demo_pb2.Cart.FromString(value)
            return demo_pb2.Cart()

        try:
            return self._run(operation)
        finally:
            elapsed = time.perf_counter() - start
            if overflow_enabled and span_context.is_valid:
                get_cart_histogram.record(elapsed, {"demo.cart.trace_id": format(span_context.trace_id, "032x")})
            else:
                get_cart_histogram.record(elapsed)

    def ping(self) -> bool:
        try:
            if self._redis is None:
                return False
            return bool(self._redis.ping())
        except Exception:
            return False
