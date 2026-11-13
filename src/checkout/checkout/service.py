# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import logging
import threading
import time
import uuid
from dataclasses import dataclass

import demo_pb2
import demo_pb2_grpc
import grpc
import httpx
from opentelemetry import baggage, propagate, trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from . import compat, flags, kafka, money

logger = logging.getLogger("checkout")
tracer = trace.get_tracer("checkout")

EMAIL_REQUEST_TIMEOUT = 1.0
BAD_PAYMENT_ADDRESS = "badAddress:50051"
_NO_DEADLINE = 10**9
_DEADLINE_SLACK = 0.05


class CheckoutError(Exception):
    pass


class CallContext:
    """The parts of Go's context.Context that PlaceOrder relies on: deadline and cancellation."""

    def __init__(self, servicer_context):
        self._ctx = servicer_context

    def remaining(self):
        remaining = self._ctx.time_remaining()
        if remaining is None or remaining > _NO_DEADLINE:
            return None
        return remaining

    def err(self):
        remaining = self.remaining()
        if remaining is not None and remaining <= 0:
            return compat.DEADLINE_EXCEEDED
        if not self._ctx.is_active():
            if remaining is not None and remaining < _DEADLINE_SLACK:
                return compat.DEADLINE_EXCEEDED
            return compat.CANCELED
        return None


@dataclass
class OrderPrep:
    order_items: list
    cart_items: list
    shipping_cost_localized: object


def _deadline_timeout(ctx):
    remaining = ctx.remaining()
    if remaining is None:
        return None
    return max(remaining, 0.001)


class CheckoutService(demo_pb2_grpc.CheckoutServiceServicer):
    def __init__(
        self,
        *,
        shipping_addr,
        email_addr,
        cart,
        product_catalog,
        currency,
        payment,
        http_client,
        kafka_broker_addr="",
        producer=None,
        channel_factory=grpc.insecure_channel,
    ):
        self.shipping_svc_addr = shipping_addr
        self.email_svc_addr = email_addr
        self.cart_svc_client = cart
        self.product_catalog_svc_client = product_catalog
        self.currency_svc_client = currency
        self.payment_svc_client = payment
        self.http_client = http_client
        self.kafka_broker_svc_addr = kafka_broker_addr
        self.kafka_producer_client = producer
        self._channel_factory = channel_factory

    def PlaceOrder(self, request, context):
        span = trace.get_current_span()
        span.set_attributes(
            {"user.id": request.user_id, "demo.user_context.selected_currency": request.user_currency}
        )

        if flags.emit_raw_pii():
            span.set_attributes(
                {
                    "user.email": request.email,
                    "demo.payment.card_number": request.credit_card.credit_card_number,
                    "demo.payment.card_cvv": int(request.credit_card.credit_card_cvv),
                }
            )

        if baggage.get_baggage("synthetic_request") == "true":
            span.set_attribute("user_agent.synthetic.type", "test")

        logger.info(
            "[PlaceOrder]", extra={"user_id": request.user_id, "user_currency": request.user_currency}
        )

        ctx = CallContext(context)
        address = request.address if request.HasField("address") else None
        order_id = str(uuid.uuid1())

        try:
            prep = self.prepare_order_items_and_shipping_quote_from_cart(
                ctx, request.user_id, request.user_currency, address
            )
        except CheckoutError as err:
            span.record_exception(err)
            context.abort(grpc.StatusCode.INTERNAL, str(err))
        span.add_event("prepared")

        total = demo_pb2.Money(currency_code=request.user_currency, units=0, nanos=0)
        total = money.sum(total, prep.shipping_cost_localized)
        for it in prep.order_items:
            mult_price = money.multiply_slow(it.cost, it.item.quantity)
            total = money.sum(total, mult_price)

        try:
            tx_id = self.charge_card(ctx, total, request.credit_card if request.HasField("credit_card") else None)
        except CheckoutError as err:
            span.record_exception(err)
            context.abort(grpc.StatusCode.INTERNAL, f"failed to charge card: {err}")

        span.add_event("charged", {"demo.payment.transaction.id": tx_id})
        logger.info("payment went through", extra={"transaction_id": tx_id})

        try:
            shipping_tracking_id = self.ship_order(ctx, address, prep.cart_items)
        except CheckoutError as err:
            span.record_exception(err)
            context.abort(grpc.StatusCode.UNAVAILABLE, f"shipping error: {err}")
        span.add_event("shipped", {"demo.shipping.tracking.id": shipping_tracking_id})

        try:
            self.empty_user_cart(ctx, request.user_id)
        except CheckoutError:
            pass

        order_result = demo_pb2.OrderResult(
            order_id=order_id,
            shipping_tracking_id=shipping_tracking_id,
            shipping_cost=prep.shipping_cost_localized,
            shipping_address=address,
            items=prep.order_items,
        )

        shipping_cost_float = compat.money_float(prep.shipping_cost_localized)
        total_price_float = compat.money_float(total)

        order_attributes = {
            "demo.order.id": order_id,
            "demo.shipping.amount": shipping_cost_float,
            "demo.order.amount": total_price_float,
            "demo.order.items.count": len(prep.order_items),
            "demo.shipping.tracking.id": shipping_tracking_id,
        }
        span.set_attributes(order_attributes)
        logger.info("order placed", extra=order_attributes)

        try:
            self.send_order_confirmation(ctx, request.email, order_result)
        except CheckoutError as err:
            logger.warning(f"failed to send order confirmation: {err}")
        else:
            logger.info("order confirmation email sent")

        if self.kafka_broker_svc_addr != "":
            logger.info("sending to postProcessor")
            self.send_to_post_processor(ctx, order_result)

        return demo_pb2.PlaceOrderResponse(order=order_result)

    def prepare_order_items_and_shipping_quote_from_cart(self, ctx, user_id, user_currency, address):
        with tracer.start_as_current_span(
            "prepareOrderItemsAndShippingQuoteFromCart",
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                cart_items = self.get_user_cart(ctx, user_id)
            except CheckoutError as err:
                raise CheckoutError(f"cart failure: {err}") from err
            try:
                order_items = self.prep_order_items(ctx, cart_items, user_currency)
            except CheckoutError as err:
                raise CheckoutError(f"failed to prepare order: {err}") from err
            try:
                shipping_usd = self.quote_shipping(ctx, address, cart_items)
            except CheckoutError as err:
                raise CheckoutError(f"shipping quote failure: {err}") from err
            try:
                shipping_price = self.convert_currency(ctx, shipping_usd, user_currency)
            except CheckoutError as err:
                raise CheckoutError(f"failed to convert shipping cost to currency: {err}") from err

            total_cart = 0
            for ci in cart_items:
                total_cart += ci.quantity

            span.set_attributes(
                {
                    "demo.shipping.amount": compat.money_float(shipping_price),
                    "demo.cart.items.count": total_cart,
                    "demo.order.items.count": len(order_items),
                }
            )
            return OrderPrep(order_items=order_items, cart_items=cart_items, shipping_cost_localized=shipping_price)

    def _post_json(self, url, payload, timeout):
        try:
            return self.http_client.post(
                url,
                content=compat.go_json_dumps(payload),
                headers={"Content-Type": "application/json"},
                timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            raise CheckoutError(f'Post "{url}": {compat.DEADLINE_EXCEEDED}') from exc
        except httpx.HTTPError as exc:
            raise CheckoutError(f'Post "{url}": {exc}') from exc

    def _shipping_request(self, ctx, path, address, items):
        payload = {
            "address": compat.proto_to_go_json(address),
            "items": [compat.proto_to_go_json(i) for i in items] or None,
        }
        url = self.shipping_svc_addr + path
        try:
            resp = self._post_json(url, payload, _deadline_timeout(ctx))
        except CheckoutError as err:
            raise CheckoutError(f"failed POST to shipping service: {err}") from err
        if resp.status_code != 200:
            raise CheckoutError(f"failed POST to shipping service: expected 200, got {resp.status_code}")
        return resp.content

    def quote_shipping(self, ctx, address, items):
        body = self._shipping_request(ctx, "/get-quote", address, items)
        try:
            quote = compat.go_object(compat.go_json_loads(body), "value", "struct { CostUsd *oteldemo.Money }")
            cost = None
            if quote is not None:
                cost = compat.go_object(compat.go_field(quote, "cost_usd"), "struct field .cost_usd", "oteldemo.Money")
            if cost is not None:
                cost_usd = demo_pb2.Money(
                    currency_code=compat.go_str(compat.go_field(cost, "currency_code"), ".cost_usd.currency_code"),
                    units=compat.go_int(compat.go_field(cost, "units"), "int64", ".cost_usd.units"),
                    nanos=compat.go_int(compat.go_field(cost, "nanos"), "int32", ".cost_usd.nanos"),
                )
        except compat.JSONDecodeError as err:
            raise CheckoutError(f"failed to unmarshal shipping quote: {err}") from err
        if cost is None:
            raise CheckoutError("shipping quote missing cost_usd field")
        return cost_usd

    def get_user_cart(self, ctx, user_id):
        try:
            cart = self.cart_svc_client.GetCart(
                demo_pb2.GetCartRequest(user_id=user_id), timeout=_deadline_timeout(ctx)
            )
        except grpc.RpcError as err:
            raise CheckoutError(f"failed to get user cart during checkout: {compat.rpc_error(err)}") from err
        return list(cart.items)

    def empty_user_cart(self, ctx, user_id):
        try:
            self.cart_svc_client.EmptyCart(demo_pb2.EmptyCartRequest(user_id=user_id), timeout=_deadline_timeout(ctx))
        except grpc.RpcError as err:
            raise CheckoutError(f"failed to empty user cart during checkout: {compat.rpc_error(err)}") from err

    def prep_order_items(self, ctx, items, user_currency):
        out = []
        for item in items:
            try:
                product = self.product_catalog_svc_client.GetProduct(
                    demo_pb2.GetProductRequest(id=item.product_id), timeout=_deadline_timeout(ctx)
                )
            except grpc.RpcError as err:
                raise CheckoutError(f"failed to get product #{compat.go_quote(item.product_id)}") from err
            try:
                price = self.convert_currency(
                    ctx, product.price_usd if product.HasField("price_usd") else None, user_currency
                )
            except CheckoutError as err:
                raise CheckoutError(
                    f"failed to convert price of {compat.go_quote(item.product_id)} to {user_currency}"
                ) from err
            out.append(demo_pb2.OrderItem(item=item, cost=price))
        return out

    def convert_currency(self, ctx, from_money, to_currency):
        req = demo_pb2.CurrencyConversionRequest(to_code=to_currency)
        if from_money is not None:
            getattr(req, "from").CopyFrom(from_money)
        try:
            return self.currency_svc_client.Convert(req, timeout=_deadline_timeout(ctx))
        except grpc.RpcError as err:
            raise CheckoutError(f"failed to convert currency: {compat.rpc_error(err)}") from err

    def charge_card(self, ctx, amount, payment_info):
        payment_service = self.payment_svc_client
        channel = None
        if flags.payment_unreachable():
            channel = self._channel_factory(BAD_PAYMENT_ADDRESS)
            payment_service = demo_pb2_grpc.PaymentServiceStub(channel)
        try:
            payment_resp = payment_service.Charge(
                demo_pb2.ChargeRequest(amount=amount, credit_card=payment_info), timeout=_deadline_timeout(ctx)
            )
        except grpc.RpcError as err:
            raise CheckoutError(f"could not charge the card: {compat.rpc_error(err)}") from err
        finally:
            if channel is not None:
                channel.close()
        return payment_resp.transaction_id

    def send_order_confirmation(self, ctx, email, order):
        payload = {"email": email, "order": compat.proto_to_go_json(order)}
        remaining = ctx.remaining()
        timeout = EMAIL_REQUEST_TIMEOUT if remaining is None else max(min(EMAIL_REQUEST_TIMEOUT, remaining), 0.001)
        url = self.email_svc_addr + "/send_order_confirmation"
        try:
            resp = self._post_json(url, payload, timeout)
        except CheckoutError as err:
            raise CheckoutError(f"failed POST to email service: {err}") from err
        if resp.status_code != 200:
            raise CheckoutError(f"failed POST to email service: expected 200, got {resp.status_code}")

    def ship_order(self, ctx, address, items):
        body = self._shipping_request(ctx, "/ship-order", address, items)
        try:
            ship = compat.go_object(compat.go_json_loads(body), "value", "struct { TrackingID string }")
            tracking_id = ""
            if ship is not None:
                tracking_id = compat.go_str(compat.go_field(ship, "tracking_id"), ".tracking_id")
        except compat.JSONDecodeError as err:
            raise CheckoutError(f"failed to unmarshal ship order response: {err}") from err
        if tracking_id == "":
            raise CheckoutError("ship order response missing tracking_id field")
        return tracking_id

    def send_to_post_processor(self, ctx, result):
        message = result.SerializeToString()
        topic = kafka.TOPIC
        partition = 0

        span = tracer.start_span(
            f"{topic} publish",
            kind=SpanKind.PRODUCER,
            attributes={
                "peer.service": "kafka",
                "network.transport": "tcp",
                "messaging.system": "kafka",
                "messaging.destination.name": topic,
                "messaging.operation": "publish",
                "messaging.kafka.destination.partition": partition,
            },
        )
        carrier = {}
        propagate.inject(carrier, context=trace.set_span_in_context(span))
        headers = [(key, value.encode("utf-8")) for key, value in carrier.items()]

        try:
            self._publish(ctx, span, topic, message, headers)
        finally:
            span.end()

    def _publish(self, ctx, span, topic, message, headers):
        producer = self.kafka_producer_client
        delivered = threading.Event()
        outcome = {}

        def on_delivery(err, msg):
            outcome["err"] = err
            outcome["msg"] = msg
            delivered.set()

        def failure(status_message):
            span.set_attributes(
                {
                    "messaging.kafka.producer.success": False,
                    "messaging.kafka.producer.duration_ms": int((time.monotonic() - start_time) * 1000),
                }
            )
            span.set_status(Status(StatusCode.ERROR, status_message))

        start_time = time.monotonic()
        while True:
            err = ctx.err()
            if err is not None:
                failure("Failed to send: " + err)
                logger.error(f"Failed to send message to Kafka within context deadline: {err}")
                return
            try:
                producer.produce(topic, message, headers, on_delivery)
                break
            except BufferError:
                time.sleep(0.01)

        while not delivered.wait(0.01):
            err = ctx.err()
            if err is not None:
                failure("Context cancelled: " + err)
                logger.warning(f"Context canceled before success message received: {err}")
                break
        else:
            kafka_err = outcome["err"]
            elapsed = time.monotonic() - start_time
            if kafka_err is None:
                offset = outcome["msg"].offset()
                if offset is None or offset < 0:
                    offset = 0
                span.set_attributes(
                    {
                        "messaging.kafka.producer.success": True,
                        "messaging.kafka.producer.duration_ms": int(elapsed * 1000),
                        "messaging.kafka.message.offset": offset,
                    }
                )
                logger.info(
                    f"Successful to write message. offset: {offset}, duration: {compat.go_duration(elapsed)}"
                )
            else:
                reason = _kafka_error_text(kafka_err)
                failure(reason)
                logger.error(f"Failed to write message: {reason}")

        ff_value = flags.kafka_queue_problems()
        if ff_value > 0:
            logger.info("Warning: FeatureFlag 'kafkaQueueProblems' is activated, overloading queue now.")
            threading.Thread(
                target=self._overload, args=(topic, message, headers, ff_value), name="kafka-overload", daemon=True
            ).start()
            logger.info(f"Done with #{ff_value} messages for overload simulation.")

    def _overload(self, topic, message, headers, count):
        def on_delivery(err, msg):
            if err is not None:
                logger.error(
                    f"Failed to write message: kafka: Failed to produce message to topic {topic}: {_kafka_error_text(err)}"
                )

        for _ in range(count):
            while True:
                try:
                    self.kafka_producer_client.produce(topic, message, headers, on_delivery)
                    break
                except BufferError:
                    time.sleep(0.01)


def _kafka_error_text(err):
    text = getattr(err, "str", None)
    return text() if callable(text) else str(err)
