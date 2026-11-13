# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
import logging
import threading

import demo_pb2

logger = logging.getLogger("product-catalog")

_COLUMNS = """SELECT p.id, p.name, p.description, p.picture,
		       p.price_currency_code, p.price_units, p.price_nanos, p.categories
		FROM catalog.products p"""

LIST_PRODUCTS_SQL = f"""
		{_COLUMNS}
		ORDER BY p.id
	"""

SEARCH_PRODUCTS_SQL = f"""
		{_COLUMNS}
		WHERE LOWER(p.name) LIKE %(pattern)s OR LOWER(p.description) LIKE %(pattern)s
		ORDER BY p.id
	"""

GET_PRODUCT_SQL = f"""
		{_COLUMNS}
		WHERE p.id = %(id)s
	"""

LOCK_SQL = "LOCK TABLE catalog.products IN ACCESS EXCLUSIVE MODE"


class ProductNotFound(Exception):
    pass


class CatalogError(Exception):
    pass


def parse_product_row(row) -> demo_pb2.Product:
    if len(row) != 8 or any(value is None for value in row):
        raise CatalogError(f"failed to scan product row: unsupported NULL or column count in {row!r}")
    product_id, name, description, picture, currency_code, units, nanos, categories_str = row
    categories = [c.strip() for c in categories_str.split(",")] if categories_str != "" else []
    return demo_pb2.Product(
        id=product_id,
        name=name,
        description=description,
        picture=picture,
        price_usd=demo_pb2.Money(currency_code=currency_code, units=units, nanos=nanos),
        categories=categories,
    )


class PostgresCatalog:
    def __init__(self, pool):
        self._pool = pool

    def _products_from_rows(self, rows) -> list:
        products = []
        for row in rows:
            try:
                products.append(parse_product_row(row))
            except CatalogError as ex:
                raise CatalogError(f"failed to get products from rows: {ex}") from ex
        logger.info(f"Found {len(products)} products from database", extra={"products": len(products)})
        return products

    def _query(self, sql: str, params=None):
        if self._pool is None:
            raise CatalogError("database connection not initialized")
        try:
            with self._pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    return cur.fetchall()
        except CatalogError:
            raise
        except Exception as ex:
            raise CatalogError(f"failed to query products: {ex}") from ex

    def list_products(self) -> list:
        return self._products_from_rows(self._query(LIST_PRODUCTS_SQL))

    def search_products(self, query: str) -> list:
        return self._products_from_rows(self._query(SEARCH_PRODUCTS_SQL, {"pattern": f"%{query.lower()}%"}))

    def get_product(self, product_id: str) -> demo_pb2.Product:
        if self._pool is None:
            raise CatalogError("database connection not initialized")
        try:
            with self._pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(GET_PRODUCT_SQL, {"id": product_id})
                    row = cur.fetchone()
        except Exception as ex:
            raise CatalogError(f"failed to scan product row: {ex}") from ex
        if row is None:
            raise ProductNotFound("product not found")
        return parse_product_row(row)

    def hold_lock(self, stop: threading.Event, seconds: float = 30) -> None:
        try:
            with self._pool.connection() as conn:
                try:
                    with conn.transaction(force_rollback=True):
                        conn.execute(LOCK_SQL)
                        logger.info("lock contention scenario active: holding ACCESS EXCLUSIVE lock on catalog.products")
                        stop.wait(seconds)
                except Exception as ex:
                    logger.error("failed to acquire lock for lock contention scenario", extra={"error": str(ex)})
        except Exception as ex:
            logger.error("failed to begin lock contention transaction", extra={"error": str(ex)})
