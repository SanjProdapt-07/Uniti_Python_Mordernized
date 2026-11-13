# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import json
import threading
from concurrent import futures

import demo_pb2
import demo_pb2_grpc
import grpc
import httpx
import pytest
from openfeature import api
from openfeature.provider.in_memory_provider import InMemoryFlag, InMemoryProvider
from opentelemetry import metrics, trace
from opentelemetry.instrumentation.grpc import GrpcInstrumentorClient, GrpcInstrumentorServer
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

SPAN_EXPORTER = InMemorySpanExporter()
_tp = TracerProvider()
_tp.add_span_processor(SimpleSpanProcessor(SPAN_EXPORTER))
trace.set_tracer_provider(_tp)
METRIC_READER = InMemoryMetricReader()
metrics.set_meter_provider(MeterProvider(metric_readers=[METRIC_READER]))

GrpcInstrumentorServer().instrument()
GrpcInstrumentorClient().instrument()
HTTPXClientInstrumentor().instrument()

try:
    from openfeature.contrib.hook.opentelemetry import TracingHook

    api.add_hooks([TracingHook()])
    HAVE_TRACING_HOOK = True
except ImportError:
    HAVE_TRACING_HOOK = False

from checkout import rpcmetrics  # noqa: E402
from checkout.__main__ import create_server  # noqa: E402
from checkout.service import CheckoutService  # noqa: E402

PRODUCTS = {
    "OLJCESPC7Z": demo_pb2.Money(currency_code="USD", units=101, nanos=960000000),
    "66VCHSJNUP": demo_pb2.Money(currency_code="USD", units=349, nanos=950000000),
}


class Backend(demo_pb2_grpc.CartServiceServicer, demo_pb2_grpc.ProductCatalogServiceServicer,
              demo_pb2_grpc.CurrencyServiceServicer, demo_pb2_grpc.PaymentServiceServicer):
    def __init__(self):
        self.calls = []
        self.cart_items = [
            demo_pb2.CartItem(product_id="OLJCESPC7Z", quantity=2),
            demo_pb2.CartItem(product_id="66VCHSJNUP", quantity=1),
        ]
        self.fail = {}
        self.convert_hook = None
        self.lock = threading.Lock()

    def _record(self, name, request, context):
        md = dict(context.invocation_metadata())
        with self.lock:
            self.calls.append((name, request, md, context.time_remaining()))
        if name in self.fail:
            code, msg = self.fail[name]
            context.abort(code, msg)

    def names(self):
        return [c[0] for c in self.calls]

    def GetCart(self, request, context):
        self._record("GetCart", request, context)
        return demo_pb2.Cart(user_id=request.user_id, items=self.cart_items)

    def EmptyCart(self, request, context):
        self._record("EmptyCart", request, context)
        return demo_pb2.Empty()

    def GetProduct(self, request, context):
        self._record("GetProduct", request, context)
        return demo_pb2.Product(id=request.id, name=request.id, price_usd=PRODUCTS[request.id])

    def Convert(self, request, context):
        self._record("Convert", request, context)
        src = getattr(request, "from")
        result = demo_pb2.Money(currency_code=request.to_code, units=src.units, nanos=src.nanos)
        if self.convert_hook is not None:
            self.convert_hook(result, context)
        return result

    def Charge(self, request, context):
        self._record("Charge", request, context)
        return demo_pb2.ChargeResponse(transaction_id="tx-123")


class HttpFake:
    def __init__(self):
        self.requests = []
        self.responses = {
            "/get-quote": lambda: httpx.Response(
                200, json={"cost_usd": {"currency_code": "USD", "units": 8, "nanos": 990000000}}
            ),
            "/ship-order": lambda: httpx.Response(200, json={"tracking_id": "TRACK-1"}),
            "/send_order_confirmation": lambda: httpx.Response(200, text=""),
        }

    def __call__(self, request):
        self.requests.append(request)
        return self.responses[request.url.path]()

    def by_path(self, path):
        return [r for r in self.requests if r.url.path == path]


class FakeProducer:
    def __init__(self):
        self.messages = []
        self.mode = "ok"
        self.buffer_full = False
        self.lock = threading.Lock()

    def produce(self, topic, value, headers, on_delivery):
        if self.buffer_full:
            raise BufferError("Local: Queue full")
        with self.lock:
            self.messages.append((topic, value, list(headers)))
        if self.mode == "ok":
            on_delivery(None, FakeMessage(len(self.messages) - 1))
        elif self.mode == "error":
            on_delivery(FakeKafkaError("Local: Message timed out"), FakeMessage(-1001))

    def close(self, timeout=5.0):
        pass


class FakeMessage:
    def __init__(self, offset):
        self._offset = offset

    def offset(self):
        return self._offset


class FakeKafkaError:
    def __init__(self, text):
        self._text = text

    def str(self):
        return self._text


def set_flags(**values):
    flags = {
        "emitRawPii": False,
        "kafkaQueueProblems": 0,
        "paymentUnreachable": False,
    }
    flags.update(values)
    api.set_provider(
        InMemoryProvider({k: InMemoryFlag("v", {"v": v}) for k, v in flags.items()})
    )


class Harness:
    def __init__(self, kafka_addr="kafka:9092"):
        self.backend = Backend()
        self.backend_server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
        for add in (
            demo_pb2_grpc.add_CartServiceServicer_to_server,
            demo_pb2_grpc.add_ProductCatalogServiceServicer_to_server,
            demo_pb2_grpc.add_CurrencyServiceServicer_to_server,
            demo_pb2_grpc.add_PaymentServiceServicer_to_server,
        ):
            add(self.backend, self.backend_server)
        backend_port = self.backend_server.add_insecure_port("127.0.0.1:0")
        self.backend_server.start()
        target = f"127.0.0.1:{backend_port}"
        interceptor = rpcmetrics.ClientMetricsInterceptor()
        self.backend_channel = rpcmetrics.instrumented_channel(target, interceptor)
        self.http = HttpFake()
        self.producer = FakeProducer()
        self.bad_channels = []

        def channel_factory(addr):
            self.bad_channels.append(addr)
            return grpc.insecure_channel(addr)

        self.svc = CheckoutService(
            shipping_addr="http://shipping:50050",
            email_addr="http://email:6060",
            cart=demo_pb2_grpc.CartServiceStub(self.backend_channel),
            product_catalog=demo_pb2_grpc.ProductCatalogServiceStub(self.backend_channel),
            currency=demo_pb2_grpc.CurrencyServiceStub(self.backend_channel),
            payment=demo_pb2_grpc.PaymentServiceStub(self.backend_channel),
            http_client=self._http_client(),
            kafka_broker_addr=kafka_addr,
            producer=self.producer,
            channel_factory=channel_factory,
        )
        self.server, _, port = create_server(self.svc, 0, max_workers=8)
        self.server.start()
        self.channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        self.client = demo_pb2_grpc.CheckoutServiceStub(self.channel)

    def _http_client(self):
        client = httpx.Client(transport=httpx.MockTransport(self.http), timeout=None)
        HTTPXClientInstrumentor.instrument_client(client)
        return client

    def close(self):
        self.channel.close()
        self.server.stop(None).wait()
        self.backend_channel.close()
        self.backend_server.stop(None).wait()


def order_request(**overrides):
    req = demo_pb2.PlaceOrderRequest(
        user_id="user-1",
        user_currency="USD",
        email="someone@example.com",
        address=demo_pb2.Address(
            street_address="1600 Amphitheatre Parkway",
            city="Mountain View",
            state="CA",
            country="United States",
            zip_code="94043",
        ),
        credit_card=demo_pb2.CreditCardInfo(
            credit_card_number="4432-8015-6152-0454",
            credit_card_cvv=672,
            credit_card_expiration_year=2030,
            credit_card_expiration_month=1,
        ),
    )
    for k, v in overrides.items():
        setattr(req, k, v)
    return req


@pytest.fixture
def harness():
    set_flags()
    SPAN_EXPORTER.clear()
    h = Harness()
    yield h
    h.close()


@pytest.fixture
def spans():
    return SPAN_EXPORTER


def body_json(request):
    return json.loads(request.content)
