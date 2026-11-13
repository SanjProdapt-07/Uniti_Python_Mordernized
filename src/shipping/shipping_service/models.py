# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

from typing import Annotated

from pydantic import AliasChoices, BaseModel, Field, StrictInt, StrictStr

U32 = Annotated[StrictInt, Field(ge=0, le=2**32 - 1)]


class CartItem(BaseModel):
    product_id: StrictStr = Field(validation_alias=AliasChoices("product_id", "productId"))
    quantity: U32


class Address(BaseModel):
    street_address: StrictStr = Field(validation_alias=AliasChoices("street_address", "streetAddress"))
    city: StrictStr
    state: StrictStr
    country: StrictStr
    zip_code: StrictStr = Field(validation_alias=AliasChoices("zip_code", "zipCode"))


class GetQuoteRequest(BaseModel):
    items: list[CartItem]
    address: Address | None = None


class ShipOrderRequest(BaseModel):
    address: Address | None = None
    items: list[CartItem]


class Money(BaseModel):
    currency_code: str
    units: int
    nanos: int


class GetQuoteResponse(BaseModel):
    cost_usd: Money | None


class ShipOrderResponse(BaseModel):
    tracking_id: str
