# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
import random

from opentelemetry import trace
from opentelemetry.trace import StatusCode

import demo_pb2
import demo_pb2_grpc

from .store import CartStore, CartStoreError

_EMPTY = demo_pb2.Empty()


def _fail(context, ex: CartStoreError):
    span = trace.get_current_span()
    span.record_exception(ex)
    span.set_status(StatusCode.ERROR, ex.details)
    context.abort(ex.code, ex.details)


class CartService(demo_pb2_grpc.CartServiceServicer):
    def __init__(self, cart_store: CartStore, bad_cart_store: CartStore, feature_client, rng=random.random):
        self._cart_store = cart_store
        self._bad_cart_store = bad_cart_store
        self._feature_client = feature_client
        self._rng = rng

    def AddItem(self, request, context):
        span = trace.get_current_span()
        span.set_attribute("user.id", request.user_id)
        span.set_attribute("demo.product.id", request.item.product_id)
        span.set_attribute("demo.product.quantity", request.item.quantity)
        try:
            self._cart_store.add_item(request.user_id, request.item.product_id, request.item.quantity)
            return _EMPTY
        except CartStoreError as ex:
            _fail(context, ex)

    def GetCart(self, request, context):
        span = trace.get_current_span()
        span.set_attribute("user.id", request.user_id)
        span.add_event("Fetch cart")
        try:
            cart = self._cart_store.get_cart(request.user_id)
            span.set_attribute("demo.cart.items.count", sum(item.quantity for item in cart.items))
            return cart
        except CartStoreError as ex:
            _fail(context, ex)

    def EmptyCart(self, request, context):
        span = trace.get_current_span()
        span.set_attribute("user.id", request.user_id)
        span.add_event("Empty cart")
        try:
            cart_failure_rate = self._feature_client.get_float_value("cartFailure", 0.0)
            if cart_failure_rate > 0 and self._rng() < cart_failure_rate:
                self._bad_cart_store.empty_cart(request.user_id)
            else:
                self._cart_store.empty_cart(request.user_id)
        except CartStoreError as ex:
            _fail(context, ex)
        return _EMPTY
