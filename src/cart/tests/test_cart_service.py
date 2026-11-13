# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
import threading
import uuid

import grpc
import pytest
from grpc_health.v1 import health_pb2, health_pb2_grpc
from openfeature import api
from openfeature.provider.in_memory_provider import InMemoryFlag, InMemoryProvider
from opentelemetry import metrics, trace
from opentelemetry.instrumentation.grpc import server_interceptor
from opentelemetry.trace import SpanKind, StatusCode

import demo_pb2
import demo_pb2_grpc
from cart.server import build_server
from cart.store import CART_FIELD_NAME, CART_TTL_SECONDS, CartStoreError, ValkeyCartStore
from cart.telemetry import HttpServerMetricsInterceptor
from conftest import metric_points


class InMemoryCartStore:
    def __init__(self):
        self._carts = {}
        self._lock = threading.Lock()

    def initialize(self):
        pass

    def add_item(self, user_id, product_id, quantity):
        with self._lock:
            cart = self._carts.get(user_id)
            if cart is None:
                cart = demo_pb2.Cart(user_id=user_id)
                self._carts[user_id] = cart
            item = next((i for i in cart.items if i.product_id == product_id), None)
            if item is None:
                cart.items.add(product_id=product_id, quantity=quantity)
            else:
                item.quantity += quantity

    def empty_cart(self, user_id):
        self._carts[user_id] = demo_pb2.Cart()

    def get_cart(self, user_id):
        return self._carts.get(user_id, demo_pb2.Cart())

    def ping(self):
        return True


class FakeRedis:
    def __init__(self, fail=False):
        self.hashes = {}
        self.strings = {}
        self.ttl = {}
        self.fail = fail
        self.kwargs = None

    def __call__(self, **kwargs):
        self.kwargs = kwargs
        return self

    def _check(self):
        if self.fail:
            import redis
            raise redis.ConnectionError("Error -2 connecting to badhost:1234. Name or service not known.")

    def ping(self):
        self._check()
        return True

    def set(self, key, value):
        self.strings[key] = value.encode() if isinstance(value, str) else value

    def get(self, key):
        return self.strings.get(key)

    def hget(self, key, field):
        self._check()
        return self.hashes.get(key, {}).get(field)

    def hset(self, key, mapping):
        self._check()
        self.hashes.setdefault(key, {}).update(mapping)

    def expire(self, key, seconds):
        self.ttl[key] = seconds


def flags(**values):
    definitions = {}
    for key, value in values.items():
        definitions[key] = InMemoryFlag("v", {"v": value})
    return InMemoryProvider(definitions)


@pytest.fixture
def feature_client():
    api.set_provider(flags(cartFailure=0.0, failedReadinessProbe=False, cartMetricCardinalityOverflow=False))
    yield api.get_client()
    api.clear_providers()


def start(cart_store, bad_store, feature_client):
    server, port = build_server(
        cart_store, bad_store, feature_client, "127.0.0.1:0",
        interceptors=[server_interceptor(), HttpServerMetricsInterceptor()],
    )
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    return server, channel


@pytest.fixture
def in_memory_server(feature_client):
    server, channel = start(InMemoryCartStore(), InMemoryCartStore(), feature_client)
    yield demo_pb2_grpc.CartServiceStub(channel), channel
    channel.close()
    server.stop(None)


def test_get_item_no_add_item_before_empty_cart_returned(in_memory_server):
    client, _ = in_memory_server
    cart = client.GetCart(demo_pb2.GetCartRequest(user_id=str(uuid.uuid4())))
    assert cart == demo_pb2.Cart()


def test_add_item_item_exists_updated(in_memory_server):
    client, _ = in_memory_server
    user_id = str(uuid.uuid4())
    request = demo_pb2.AddItemRequest(user_id=user_id, item=demo_pb2.CartItem(product_id="1", quantity=1))
    client.AddItem(request)
    client.AddItem(request)
    cart = client.GetCart(demo_pb2.GetCartRequest(user_id=user_id))
    assert cart.user_id == user_id
    assert len(cart.items) == 1
    assert cart.items[0].quantity == 2
    client.EmptyCart(demo_pb2.EmptyCartRequest(user_id=user_id))


def test_add_item_new_inserted(in_memory_server):
    client, _ = in_memory_server
    user_id = str(uuid.uuid4())
    client.AddItem(demo_pb2.AddItemRequest(user_id=user_id, item=demo_pb2.CartItem(product_id="1", quantity=1)))
    cart = client.GetCart(demo_pb2.GetCartRequest(user_id=user_id))
    assert cart.user_id == user_id
    assert len(cart.items) == 1
    client.EmptyCart(demo_pb2.EmptyCartRequest(user_id=user_id))
    cart = client.GetCart(demo_pb2.GetCartRequest(user_id=user_id))
    assert len(cart.items) == 0


def _server_span(spans, name):
    matches = [s for s in spans.get_finished_spans() if s.name == name and s.kind == SpanKind.SERVER]
    assert matches, [s.name for s in spans.get_finished_spans()]
    return matches[-1]


def test_spans_attributes_events_and_http_metric(in_memory_server, spans, metric_reader):
    client, _ = in_memory_server
    client.AddItem(demo_pb2.AddItemRequest(user_id="u1", item=demo_pb2.CartItem(product_id="OLJCESPC7Z", quantity=3)))
    client.AddItem(demo_pb2.AddItemRequest(user_id="u1", item=demo_pb2.CartItem(product_id="66VCHSJNUP", quantity=2)))
    client.GetCart(demo_pb2.GetCartRequest(user_id="u1"))
    client.EmptyCart(demo_pb2.EmptyCartRequest(user_id="u1"))

    add = _server_span(spans, "oteldemo.CartService/AddItem")
    assert add.attributes["user.id"] == "u1"
    assert add.attributes["demo.product.id"] == "66VCHSJNUP"
    assert add.attributes["demo.product.quantity"] == 2

    get = _server_span(spans, "oteldemo.CartService/GetCart")
    assert get.attributes["demo.cart.items.count"] == 5
    assert [e.name for e in get.events] == ["Fetch cart"]

    empty = _server_span(spans, "oteldemo.CartService/EmptyCart")
    assert empty.attributes["user.id"] == "u1"
    assert "Empty cart" in [e.name for e in empty.events]

    points = metric_points(metric_reader, "http.server.request.duration")
    routes = {p.attributes["http.route"] for p in points}
    assert "/oteldemo.CartService/AddItem" in routes
    add_point = next(p for p in points if p.attributes["http.route"] == "/oteldemo.CartService/AddItem")
    assert add_point.attributes["http.request.method"] == "POST"
    assert add_point.attributes["http.response.status_code"] == 200
    assert add_point.count >= 2


def test_valkey_layout_ttl_and_compatibility():
    fake = FakeRedis()
    api.set_provider(flags(cartMetricCardinalityOverflow=False))
    store = ValkeyCartStore("valkey-cart:6379", api.get_client(), "secret", False, client_factory=fake)
    store.initialize()
    assert fake.kwargs["host"] == "valkey-cart"
    assert fake.kwargs["port"] == 6379
    assert fake.kwargs["password"] == "secret"
    assert fake.strings["cart"] == b"OK"

    store.add_item("user-1", "P1", 1)
    store.add_item("user-1", "P1", 2)
    store.add_item("user-1", "P2", 1)
    stored = demo_pb2.Cart.FromString(fake.hashes["user-1"][CART_FIELD_NAME])
    assert stored.user_id == "user-1"
    assert [(i.product_id, i.quantity) for i in stored.items] == [("P1", 3), ("P2", 1)]
    assert fake.ttl["user-1"] == CART_TTL_SECONDS == 3600
    assert store.get_cart("user-1") == stored

    store.empty_cart("user-1")
    assert fake.hashes["user-1"][CART_FIELD_NAME] == b""
    assert store.get_cart("user-1") == demo_pb2.Cart()
    assert store.get_cart("missing") == demo_pb2.Cart()
    assert store.ping()


def test_store_unreachable_raises_failed_precondition():
    api.set_provider(flags(cartMetricCardinalityOverflow=False))
    store = ValkeyCartStore("badhost:1234", api.get_client(), client_factory=FakeRedis(fail=True))
    with pytest.raises(CartStoreError) as error:
        store.empty_cart("u")
    assert error.value.code == grpc.StatusCode.FAILED_PRECONDITION
    assert error.value.details.startswith("Can't access cart storage. ")
    assert "Wasn't able to connect to redis" in error.value.details
    assert not store.ping()


def test_initialize_fails_after_retries():
    api.set_provider(flags())
    store = ValkeyCartStore("badhost:1234", api.get_client(), connect_retry=1, client_factory=FakeRedis(fail=True))
    with pytest.raises(ConnectionError, match="Wasn't able to connect to redis"):
        store.initialize()


def test_get_cart_latency_metric_trace_id_only_when_flag_on(metric_reader):
    fake = FakeRedis()
    api.set_provider(flags(cartMetricCardinalityOverflow=True))
    store = ValkeyCartStore("valkey:6379", api.get_client(), client_factory=fake)
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("parent") as span:
        store.get_cart("u")
        trace_id = format(span.get_span_context().trace_id, "032x")
    with_id = [p for p in metric_points(metric_reader, "demo.cart.get_cart.latency")
               if p.attributes.get("demo.cart.trace_id") == trace_id]
    assert len(with_id) == 1

    api.set_provider(flags(cartMetricCardinalityOverflow=False))
    with tracer.start_as_current_span("parent2") as span:
        store.get_cart("u")
        other = format(span.get_span_context().trace_id, "032x")
    assert not [p for p in metric_points(metric_reader, "demo.cart.get_cart.latency")
                if p.attributes.get("demo.cart.trace_id") == other]
    store.add_item("u", "P", 1)
    assert metric_points(metric_reader, "demo.cart.add_item.latency")


def test_cart_failure_flag_routes_empty_cart_to_bad_store(spans):
    api.set_provider(flags(cartFailure=1.0, failedReadinessProbe=False))
    good = InMemoryCartStore()
    bad = ValkeyCartStore("badhost:1234", api.get_client(), client_factory=FakeRedis(fail=True))
    server, channel = start(good, bad, api.get_client())
    try:
        client = demo_pb2_grpc.CartServiceStub(channel)
        with pytest.raises(grpc.RpcError) as error:
            client.EmptyCart(demo_pb2.EmptyCartRequest(user_id="u"))
        assert error.value.code() == grpc.StatusCode.FAILED_PRECONDITION
        assert error.value.details().startswith("Can't access cart storage.")
        span = _server_span(spans, "oteldemo.CartService/EmptyCart")
        assert span.status.status_code == StatusCode.ERROR
        assert "exception" in [e.name for e in span.events]
    finally:
        channel.close()
        server.stop(None)


def test_cart_failure_off_uses_good_store():
    api.set_provider(flags(cartFailure=0.0))
    bad = ValkeyCartStore("badhost:1234", api.get_client(), client_factory=FakeRedis(fail=True))
    server, channel = start(InMemoryCartStore(), bad, api.get_client())
    try:
        demo_pb2_grpc.CartServiceStub(channel).EmptyCart(demo_pb2.EmptyCartRequest(user_id="u"))
    finally:
        channel.close()
        server.stop(None)


@pytest.mark.parametrize("probe_failed,expected", [
    (False, health_pb2.HealthCheckResponse.SERVING),
    (True, health_pb2.HealthCheckResponse.NOT_SERVING),
])
def test_health_readiness_follows_failed_readiness_probe(probe_failed, expected):
    api.set_provider(flags(failedReadinessProbe=probe_failed))
    server, channel = start(InMemoryCartStore(), InMemoryCartStore(), api.get_client())
    try:
        health = health_pb2_grpc.HealthStub(channel)
        assert health.Check(health_pb2.HealthCheckRequest()).status == expected
        assert health.Check(health_pb2.HealthCheckRequest(service="oteldemo.CartService")).status == expected
        watched = list(health.Watch(health_pb2.HealthCheckRequest(service="oteldemo.CartService")))
        assert [w.status for w in watched] == [expected]
        with pytest.raises(grpc.RpcError) as error:
            health.Check(health_pb2.HealthCheckRequest(service="unknown"))
        assert error.value.code() == grpc.StatusCode.UNKNOWN
    finally:
        channel.close()
        server.stop(None)


def test_feature_flag_metrics_hook():
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from cart.featureflags import MetricsHook

    reader = InMemoryMetricReader()
    hook = MetricsHook(meter_provider=MeterProvider(metric_readers=[reader]))
    api.set_provider(flags(cartFailure=0.5))
    client = api.get_client()
    client.add_hooks([hook])
    assert client.get_float_value("cartFailure", 0.0) == 0.5
    requests = metric_points(reader, "feature_flag.evaluation_requests_total")
    success = metric_points(reader, "feature_flag.evaluation_success_total")
    active = metric_points(reader, "feature_flag.evaluation_active_count")
    assert requests[0].attributes["feature_flag.key"] == "cartFailure" and requests[0].value == 1
    assert success[0].attributes["feature_flag.result.variant"] == "v"
    assert active[0].value == 0
    assert metrics.get_meter_provider() is not None
