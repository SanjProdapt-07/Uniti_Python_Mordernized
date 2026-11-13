# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import json
import logging

import grpc
import pytest

import demo_pb2
from frontend_server.nextcompat import generate_etag

from conftest import body_of

EXPLORASCOPE = {
    "id": "OLJCESPC7Z",
    "name": "National Park Foundation Explorascope",
    "description": "The Explorascope",
    "picture": "NationalParkFoundationExplorascope.jpg",
    "priceUsd": {"currencyCode": "USD", "units": 101, "nanos": 960000000},
    "categories": ["telescopes"],
}
STARSENSE = {
    "id": "66VCHSJNUP",
    "name": "Starsense Explorer",
    "description": "Starsense",
    "picture": "StarsenseExplorer.jpg",
    "priceUsd": {"currencyCode": "USD", "units": 349, "nanos": 950000000},
    "categories": ["telescopes"],
}


def eur(product):
    price = product["priceUsd"]
    return {**product, "priceUsd": {"currencyCode": "EUR", "units": price["units"] * 2, "nanos": price["nanos"] // 2}}


def test_products_list_usd_has_no_conversion(client, fakes):
    response = client.get("/api/products?currencyCode=USD")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json; charset=utf-8"
    assert response.content == json.dumps([EXPLORASCOPE, STARSENSE], separators=(",", ":")).encode()
    assert fakes.calls("Convert") == []


def test_products_list_without_currency_does_not_convert(client, fakes):
    assert body_of(client.get("/api/products")) == [EXPLORASCOPE, STARSENSE]
    assert fakes.calls("Convert") == []


def test_products_list_converts(client, fakes):
    assert body_of(client.get("/api/products?currencyCode=EUR")) == [eur(EXPLORASCOPE), eur(STARSENSE)]
    assert [c.to_code for c in fakes.calls("Convert")] == ["EUR", "EUR"]


def test_product_detail(client):
    response = client.get("/api/products/OLJCESPC7Z?currencyCode=EUR")
    assert response.status_code == 200
    assert body_of(response) == eur(EXPLORASCOPE)
    assert list(body_of(response)) == ["id", "name", "description", "picture", "priceUsd", "categories"]


def test_product_without_price_omits_price_like_ts_proto(client):
    assert body_of(client.get("/api/products/NOPRICE")) == {
        "id": "NOPRICE",
        "name": "No price",
        "description": "",
        "picture": "",
        "categories": [],
    }


def test_unknown_product_is_plain_500(client, caplog):
    with caplog.at_level(logging.ERROR, logger="frontend"):
        response = client.get("/api/products/MISSING")
    assert response.status_code == 500
    assert response.content == b"Internal Server Error"
    assert "content-type" not in response.headers
    record = next(r for r in caplog.records if r.getMessage() == "API request failed")
    assert getattr(record, "http.response.status_code") == 500
    assert getattr(record, "url.path") == "/api/products/MISSING"


def test_cart_add_get_and_empty(client, fakes):
    added = client.post("/api/cart?currencyCode=EUR", json={"item": {"productId": "OLJCESPC7Z", "quantity": 2}, "userId": "u1"})
    assert added.status_code == 200
    assert body_of(added) == {"userId": "u1", "items": [{"productId": "OLJCESPC7Z", "quantity": 2}]}
    assert fakes.calls("AddItem")[0] == demo_pb2.AddItemRequest(
        user_id="u1", item=demo_pb2.CartItem(product_id="OLJCESPC7Z", quantity=2)
    )

    cart = client.get("/api/cart?sessionId=u1&currencyCode=EUR")
    assert body_of(cart) == {
        "userId": "u1",
        "items": [{"productId": "OLJCESPC7Z", "quantity": 2, "product": eur(EXPLORASCOPE)}],
    }

    emptied = client.request("DELETE", "/api/cart", json={"userId": "u1"})
    assert emptied.status_code == 204
    assert emptied.content == b""
    assert fakes.calls("EmptyCart")[0].user_id == "u1"
    assert body_of(client.get("/api/cart?sessionId=u1")) == {"userId": "u1", "items": []}


def test_cart_get_without_session_like_load_generator(client, fakes):
    assert body_of(client.get("/api/cart")) == {"userId": "", "items": []}
    assert fakes.calls("GetCart")[0].user_id == ""


def test_cart_post_without_user_id_uses_empty_string(client, fakes):
    assert client.post("/api/cart", json={"item": {"productId": "OLJCESPC7Z", "quantity": 1}}).status_code == 200
    assert fakes.calls("AddItem")[0].user_id == ""


def test_invalid_json_body_is_400(client):
    response = client.post("/api/cart", content=b"{nope", headers={"content-type": "application/json"})
    assert response.status_code == 400
    assert response.content == b"Invalid JSON"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("PUT", "/api/cart"),
        ("GET", "/api/checkout"),
        ("POST", "/api/currency"),
        ("POST", "/api/data"),
        ("POST", "/api/recommendations"),
        ("POST", "/api/shipping"),
        ("DELETE", "/api/products"),
        ("POST", "/api/products/OLJCESPC7Z"),
    ],
)
def test_unsupported_methods_are_405_with_empty_body(client, method, path):
    response = client.request(method, path)
    assert response.status_code == 405
    assert response.content == b""
    assert response.headers["etag"] == generate_etag("")


def test_currency(client):
    response = client.get("/api/currency")
    assert response.status_code == 200
    assert response.content == b'["USD","EUR","CHF"]'


def test_data_from_browser_comma_joined(client, fakes):
    assert body_of(client.get("/api/data?contextKeys=telescopes%2Cbinoculars")) == [
        {"redirectUrl": "/product/telescopes", "text": "Ad for telescopes"},
        {"redirectUrl": "/product/binoculars", "text": "Ad for binoculars"},
    ]
    assert list(fakes.calls("GetAds")[0].context_keys) == ["telescopes", "binoculars"]


def test_data_repeated_and_missing_keys(client, fakes):
    client.get("/api/data?contextKeys=a&contextKeys=b")
    client.get("/api/data")
    assert [list(c.context_keys) for c in fakes.calls("GetAds")] == [["a", "b"], []]


def test_data_trailing_slash_redirects_like_next(client):
    response = client.get("/api/data/?contextKeys=telescopes")
    assert response.status_code == 308
    assert response.headers["location"] == "/api/data?contextKeys=telescopes"
    assert response.headers["refresh"] == "0;url=/api/data?contextKeys=telescopes"
    followed = client.get("/api/data/?contextKeys=telescopes", follow_redirects=True)
    assert body_of(followed) == [{"redirectUrl": "/product/telescopes", "text": "Ad for telescopes"}]


def test_recommendations_returns_first_four_products(client, fakes):
    response = client.get("/api/recommendations?productIds=OLJCESPC7Z&productIds=66VCHSJNUP&sessionId=u1&currencyCode=USD")
    assert body_of(response) == [STARSENSE, EXPLORASCOPE, STARSENSE, EXPLORASCOPE]
    request = fakes.calls("ListRecommendations")[0]
    assert request.user_id == "u1"
    assert list(request.product_ids) == ["OLJCESPC7Z", "66VCHSJNUP"]


def test_recommendations_single_value_is_iterated_like_a_js_string(client, fakes):
    client.get("/api/recommendations?productIds=AB%2CC")
    assert list(fakes.calls("ListRecommendations")[0].product_ids) == ["A", "B", ",", "C"]


def test_shipping(client, fakes, shipping):
    items = [{"productId": "OLJCESPC7Z", "quantity": 2}]
    address = {"streetAddress": "1600 Amphitheatre Parkway", "city": "Mountain View", "state": "CA", "country": "USA", "zipCode": "94043"}
    response = client.get(
        "/api/shipping",
        params={"itemList": json.dumps(items), "currencyCode": "EUR", "address": json.dumps(address)},
    )
    assert response.status_code == 200
    assert body_of(response) == {"currencyCode": "EUR", "units": 16, "nanos": 495000000}
    sent = shipping.requests[0]
    assert str(sent.url) == "http://shipping:50050/get-quote"
    assert sent.method == "POST"
    assert sent.headers["content-type"] == "application/json"
    assert sent.content == (
        b'{"items":[{"product_id":"OLJCESPC7Z","quantity":2}],'
        b'"address":{"street_address":"1600 Amphitheatre Parkway","city":"Mountain View",'
        b'"state":"CA","country":"USA","zip_code":"94043"}}'
    )
    convert = fakes.calls("Convert")[0]
    assert convert.to_code == "EUR"
    assert getattr(convert, "from") == demo_pb2.Money(currency_code="USD", units=8, nanos=990000000)


def test_shipping_defaults_to_usd_and_still_converts(client, fakes):
    response = client.get("/api/shipping", params={"itemList": "[]", "address": "{}"})
    assert body_of(response) == {"currencyCode": "USD", "units": 16, "nanos": 495000000}
    assert fakes.calls("Convert")[0].to_code == "USD"


def test_shipping_errors_are_plain_500(client, shipping):
    assert client.get("/api/shipping").status_code == 500
    shipping.status = 503
    response = client.get("/api/shipping", params={"itemList": "[]", "address": "{}"})
    assert response.status_code == 500
    assert response.content == b"Internal Server Error"


CHECKOUT_BODY = {
    "userId": "u2",
    "userCurrency": "USD",
    "address": {
        "streetAddress": "1600 Amphitheatre Parkway",
        "city": "Mountain View",
        "state": "CA",
        "country": "USA",
        "zipCode": "94043",
    },
    "email": "someone@example.com",
    "creditCard": {
        "creditCardNumber": "4432-8015-6152-0454",
        "creditCardCvv": 672,
        "creditCardExpirationYear": 2039,
        "creditCardExpirationMonth": 1,
    },
}


def test_checkout_success_shape(client, fakes):
    client.post("/api/cart", json={"userId": "u2", "item": {"productId": "OLJCESPC7Z", "quantity": 3}})
    response = client.post("/api/checkout?currencyCode=EUR", json=CHECKOUT_BODY)
    assert response.status_code == 200
    body = body_of(response)
    assert body == {
        "orderId": "order-1",
        "shippingTrackingId": "track-1",
        "shippingCost": {"currencyCode": "USD", "units": 8, "nanos": 990000000},
        "shippingAddress": CHECKOUT_BODY["address"],
        "items": [
            {
                "cost": {"currencyCode": "USD", "units": 101, "nanos": 960000000},
                "item": {"productId": "OLJCESPC7Z", "quantity": 3, "product": eur(EXPLORASCOPE)},
            }
        ],
    }
    assert list(body) == ["orderId", "shippingTrackingId", "shippingCost", "shippingAddress", "items"]
    sent = fakes.calls("PlaceOrder")[0]
    assert sent.credit_card.credit_card_cvv == 672
    assert sent.address.zip_code == "94043"
    assert sent.email == "someone@example.com"


def test_checkout_item_without_cost_omits_cost(client, fakes):
    fakes.order_items = [demo_pb2.OrderItem(item=demo_pb2.CartItem(product_id="OLJCESPC7Z", quantity=1))]
    body = body_of(client.post("/api/checkout", json=CHECKOUT_BODY))
    assert body["items"] == [{"item": {"productId": "OLJCESPC7Z", "quantity": 1, "product": EXPLORASCOPE}}]


def test_checkout_payment_declined_is_422(client, fakes):
    fakes.checkout_error = (grpc.StatusCode.INTERNAL, "failed to charge card: could not charge the card: rpc error: code = Unknown desc = Payment request failed. Invalid token. app.loyalty.level=gold")
    response = client.post("/api/checkout?currencyCode=USD", json=CHECKOUT_BODY)
    assert response.status_code == 422
    assert body_of(response) == {
        "error": "Your payment could not be processed. Please check your card details and try again.",
        "code": "PAYMENT_FAILED",
    }


@pytest.mark.parametrize(
    "details",
    [
        "failed to charge card: could not charge the card: rpc error: code = Unavailable desc = connection refused",
        "failed to charge card: rpc error: code = Internal desc = boom",
        "failed to prepare order: failed to get product #\"OLJCESPC7Z\"",
    ],
)
def test_checkout_other_failures_are_generic_500(client, fakes, details):
    fakes.checkout_error = (grpc.StatusCode.INTERNAL, details)
    response = client.post("/api/checkout?currencyCode=USD", json=CHECKOUT_BODY)
    assert response.status_code == 500
    assert body_of(response) == {"error": "Failed to place order."}


def test_checkout_logs_order_id(client, fakes, caplog):
    with caplog.at_level(logging.INFO, logger="frontend"):
        client.post("/api/checkout", json=CHECKOUT_BODY)
    record = next(r for r in caplog.records if r.getMessage() == "Order placed successfully")
    assert getattr(record, "demo.order.id") == "order-1"
    completed = next(r for r in caplog.records if r.getMessage() == "API request completed")
    assert getattr(completed, "http.request.method") == "POST"
    assert getattr(completed, "url.path") == "/api/checkout"
    assert getattr(completed, "http.response.status_code") == 200


def test_json_responses_carry_next_etag_and_honour_if_none_match(client):
    first = client.get("/api/currency")
    assert first.headers["etag"] == generate_etag('["USD","EUR","CHF"]')
    second = client.get("/api/currency", headers={"if-none-match": first.headers["etag"]})
    assert second.status_code == 304
    assert second.content == b""

