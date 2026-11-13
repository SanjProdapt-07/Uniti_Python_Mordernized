# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

from dataclasses import dataclass
from typing import Protocol

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

_tracer = trace.get_tracer("Npgsql")

ORDER_SQL = 'INSERT INTO accounting."order" (order_id)\nVALUES (%s)'
ORDER_ITEM_SQL = (
    "INSERT INTO accounting.orderitem (item_cost_currency_code, item_cost_units, item_cost_nanos, "
    "product_id, quantity, order_id)\nVALUES (%s, %s, %s, %s, %s, %s)"
)
SHIPPING_SQL = (
    "INSERT INTO accounting.shipping (shipping_tracking_id, shipping_cost_currency_code, "
    "shipping_cost_units, shipping_cost_nanos, street_address, city, state, country, zip_code, "
    "order_id)\nVALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)
UNIQUE_VIOLATION = "23505"
DEFAULT_PORT = 5432


class DuplicateOrderError(Exception):
    pass


@dataclass(frozen=True)
class OrderEntity:
    order_id: str


@dataclass(frozen=True)
class OrderItemEntity:
    item_cost_currency_code: str
    item_cost_units: int
    item_cost_nanos: int
    product_id: str
    quantity: int
    order_id: str


@dataclass(frozen=True)
class ShippingEntity:
    shipping_tracking_id: str
    shipping_cost_currency_code: str
    shipping_cost_units: int
    shipping_cost_nanos: int
    street_address: str
    city: str
    state: str
    country: str
    zip_code: str
    order_id: str


@dataclass(frozen=True)
class OrderRows:
    order: OrderEntity
    items: list[OrderItemEntity]
    shipping: ShippingEntity

    def statements(self) -> list[tuple[str, tuple]]:
        statements = [(ORDER_SQL, (self.order.order_id,))]
        for item in sorted(self.items, key=lambda i: (i.product_id, i.order_id)):
            statements.append(
                (
                    ORDER_ITEM_SQL,
                    (
                        item.item_cost_currency_code,
                        item.item_cost_units,
                        item.item_cost_nanos,
                        item.product_id,
                        item.quantity,
                        item.order_id,
                    ),
                )
            )
        s = self.shipping
        statements.append(
            (
                SHIPPING_SQL,
                (
                    s.shipping_tracking_id,
                    s.shipping_cost_currency_code,
                    s.shipping_cost_units,
                    s.shipping_cost_nanos,
                    s.street_address,
                    s.city,
                    s.state,
                    s.country,
                    s.zip_code,
                    s.order_id,
                ),
            )
        )
        return statements


def _require(message, field: str, owner: str):
    if not message.HasField(field):
        raise AttributeError(f"Object reference not set to an instance of an object ({owner}.{field} is not set).")
    return getattr(message, field)


def build_rows(order) -> OrderRows:
    items: list[OrderItemEntity] = []
    tracked: set[tuple[str, str]] = set()
    for item in order.items:
        cost = _require(item, "cost", "OrderItem")
        cart_item = _require(item, "item", "OrderItem")
        key = (cart_item.product_id, order.order_id)
        if key in tracked:
            raise ValueError(
                "The instance of entity type 'OrderItemEntity' cannot be tracked because another instance "
                f"with the key value '{{ProductId: {key[0]}, OrderId: {key[1]}}}' is already being tracked."
            )
        tracked.add(key)
        items.append(
            OrderItemEntity(
                item_cost_currency_code=cost.currency_code,
                item_cost_units=cost.units,
                item_cost_nanos=cost.nanos,
                product_id=cart_item.product_id,
                quantity=cart_item.quantity,
                order_id=order.order_id,
            )
        )
    shipping_cost = _require(order, "shipping_cost", "OrderResult")
    address = _require(order, "shipping_address", "OrderResult")
    shipping = ShippingEntity(
        shipping_tracking_id=order.shipping_tracking_id,
        shipping_cost_currency_code=shipping_cost.currency_code,
        shipping_cost_units=shipping_cost.units,
        shipping_cost_nanos=shipping_cost.nanos,
        street_address=address.street_address,
        city=address.city,
        state=address.state,
        country=address.country,
        zip_code=address.zip_code,
        order_id=order.order_id,
    )
    return OrderRows(order=OrderEntity(order.order_id), items=items, shipping=shipping)


class OrderRepository(Protocol):
    def save(self, rows: OrderRows) -> None: ...


_KEYWORDS = {
    "host": "host",
    "server": "host",
    "port": "port",
    "database": "dbname",
    "db": "dbname",
    "username": "user",
    "user name": "user",
    "userid": "user",
    "user id": "user",
    "user": "user",
    "password": "password",
    "pwd": "password",
    "psw": "password",
    "timeout": "connect_timeout",
    "ssl mode": "sslmode",
    "sslmode": "sslmode",
    "application name": "application_name",
    "applicationname": "application_name",
}


def _split_pairs(connection_string: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    quote: str | None = None
    for ch in connection_string:
        if quote is not None:
            current.append(ch)
            if ch == quote:
                quote = None
        elif ch in "'\"" and "".join(current).rstrip().endswith("="):
            quote = ch
            current.append(ch)
        elif ch == ";":
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts


def parse_connection_string(connection_string: str) -> tuple[dict[str, str], str]:
    params: dict[str, str] = {}
    public_parts: list[str] = []
    for part in _split_pairs(connection_string):
        if not part.strip():
            continue
        name, sep, value = part.partition("=")
        if not sep:
            raise ValueError(f"Format of the initialization string does not conform to specification at '{part}'.")
        key = " ".join(name.strip().lower().split())
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        mapped = _KEYWORDS.get(key)
        if mapped is not None:
            if mapped == "sslmode":
                value = value.lower()
            params[mapped] = value
        if mapped != "password":
            public_parts.append(part.strip())
    return params, ";".join(public_parts)


class PostgresOrderRepository:
    def __init__(self, connection_string: str, connect=None):
        self._params, self._data_source = parse_connection_string(connection_string)
        if connect is None:
            import psycopg

            connect = psycopg.connect
        self._connect = connect
        self._connection = None

    def _get_connection(self):
        if self._connection is None or self._connection.closed:
            self._connection = self._connect(autocommit=True, **self._params)
        return self._connection

    def _discard_broken_connection(self) -> None:
        if self._connection is not None and (self._connection.closed or self._connection.broken):
            try:
                self._connection.close()
            finally:
                self._connection = None

    def _enrich(self, span, connection) -> None:
        if not span.is_recording():
            return
        span.set_attribute("db.system.name", "postgresql")
        span.set_attribute("db.namespace", self._params.get("dbname") or self._params.get("user", ""))
        span.set_attribute("server.address", self._params.get("host", "localhost"))
        port = int(self._params.get("port", DEFAULT_PORT))
        if port != DEFAULT_PORT:
            span.set_attribute("server.port", port)
        span.set_attribute("db.npgsql.data_source", self._data_source)
        backend_pid = getattr(getattr(connection, "info", None), "backend_pid", None)
        if backend_pid is not None:
            span.set_attribute("db.npgsql.connection_id", backend_pid)

    def save(self, rows: OrderRows) -> None:
        statements = rows.statements()
        try:
            connection = self._get_connection()
            with connection.transaction():
                span = _tracer.start_span("postgresql", kind=SpanKind.CLIENT)
                span.set_attribute("db.query.text", ";\n".join(sql for sql, _ in statements))
                try:
                    self._enrich(span, connection)
                    with connection.pipeline(), connection.cursor() as cursor:
                        for sql, params in statements:
                            cursor.execute(sql, params)
                except Exception as exc:
                    sqlstate = getattr(exc, "sqlstate", None)
                    span.record_exception(exc)
                    span.set_attribute("error.type", sqlstate or f"{type(exc).__module__}.{type(exc).__qualname__}")
                    if sqlstate:
                        span.set_attribute("db.response.status_code", sqlstate)
                    span.set_status(Status(StatusCode.ERROR, sqlstate or str(exc)))
                    raise
                finally:
                    span.end()
        except Exception as exc:
            self._discard_broken_connection()
            if getattr(exc, "sqlstate", None) == UNIQUE_VIOLATION:
                raise DuplicateOrderError(str(exc)) from exc
            raise
