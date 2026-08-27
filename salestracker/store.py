#!/usr/bin/env python3
"""SQLite persistence, schema, and migrations."""

from __future__ import annotations

import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from salestracker.models import (
    DEFAULT_DB,
    MONEY_QUANT,
    Order,
    Product,
    Summary,
    TrackerError,
    _now,
    parse_money,
    parse_qty,
    parse_quantity,
)

SCHEMA_VERSION = 1


class SalesTracker:
    """SQLite-backed catalog and order list."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else DEFAULT_DB
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        try:
            self._init_schema()
        except BaseException:
            # Never leave a half-opened tracker holding the write lock; the
            # caller has no object to close() when __init__ raises.
            self._conn.close()
            raise

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> SalesTracker:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _schema_version(self) -> int:
        row = self._conn.execute("PRAGMA user_version").fetchone()
        return int(row[0])

    def _init_schema(self) -> None:
        version = self._schema_version()
        if version > SCHEMA_VERSION:
            raise TrackerError(
                f"This ledger is at schema version {version}; "
                f"this app only understands up to {SCHEMA_VERSION}."
            )
        migrations = (
            (1, self._migrate_to_v1),
        )
        for target, migrator in migrations:
            if version >= target:
                continue
            with self._conn:
                migrator()
                self._conn.execute(f"PRAGMA user_version = {int(target)}")
            version = target

    def _migrate_to_v1(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                unit TEXT NOT NULL DEFAULT 'each',
                unit_price TEXT NOT NULL,
                sku TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL REFERENCES products(id),
                purchaser TEXT NOT NULL,
                quantity_ordered TEXT NOT NULL,
                quantity_received TEXT NOT NULL DEFAULT '0',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_purchaser ON orders(purchaser)"
        )
        self._migrate_legacy_sales()

    def _migrate_legacy_sales(self) -> None:
        tables = {
            row[0]
            for row in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "sales" not in tables:
            return
        rows = self._conn.execute("SELECT * FROM sales ORDER BY id").fetchall()
        if not rows:
            self._conn.execute("DROP TABLE sales")
            return
        product_ids: dict[str, int] = {}
        for row in rows:
            item = str(row["item"]).strip()
            key = item.casefold()
            if key not in product_ids:
                product_ids[key] = self._migration_product_id(
                    item, row["unit_price"]
                )
            ordered = Decimal(str(row["quantity"]))
            created = row["created_at"] if "created_at" in row.keys() else _now()
            self._conn.execute(
                """
                INSERT INTO orders (
                    product_id, purchaser, quantity_ordered, quantity_received,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    product_ids[key],
                    str(row["customer"]).strip(),
                    str(ordered),
                    "0",
                    created,
                    created,
                ),
            )
        self._conn.execute("DROP TABLE sales")

    def _migration_product_id(self, name: str, unit_price: object) -> int:
        """Product id for a legacy row, without committing mid-migration.

        Reuses a product of the same name so a migration interrupted partway
        can be retried on the next open instead of failing on a duplicate.
        """
        payload = self._validated_product(
            name=name, unit="each", unit_price=unit_price, sku="", notes=""
        )
        existing = self._conn.execute(
            "SELECT id FROM products WHERE name = ? COLLATE NOCASE",
            (payload["name"],),
        ).fetchone()
        if existing:
            return int(existing["id"])
        cursor = self._conn.execute(
            """
            INSERT INTO products (name, unit, unit_price, sku, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                payload["name"],
                payload["unit"],
                str(payload["unit_price"]),
                payload["sku"],
                payload["notes"],
                _now(),
            ),
        )
        return int(cursor.lastrowid)

    def add_product(
        self,
        *,
        name: str,
        unit: str = "each",
        unit_price: object = "0",
        sku: str = "",
        notes: str = "",
    ) -> Product:
        payload = self._validated_product(
            name=name, unit=unit, unit_price=unit_price, sku=sku, notes=notes
        )
        existing = self._conn.execute(
            "SELECT id FROM products WHERE name = ? COLLATE NOCASE",
            (payload["name"],),
        ).fetchone()
        if existing:
            raise TrackerError(
                f"A product named {payload['name']!r} is already on file."
            )
        cursor = self._conn.execute(
            """
            INSERT INTO products (name, unit, unit_price, sku, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                payload["name"],
                payload["unit"],
                str(payload["unit_price"]),
                payload["sku"],
                payload["notes"],
                _now(),
            ),
        )
        self._conn.commit()
        return self.get_product(cursor.lastrowid)

    def list_products(self) -> list[Product]:
        rows = self._conn.execute(
            "SELECT * FROM products ORDER BY name COLLATE NOCASE, id"
        ).fetchall()
        return [self._row_to_product(row) for row in rows]

    def get_product(self, product_id: int) -> Product:
        row = self._conn.execute(
            "SELECT * FROM products WHERE id = ?", (product_id,)
        ).fetchone()
        if row is None:
            raise TrackerError(f"No product with id {product_id}.")
        return self._row_to_product(row)

    def find_product(self, name_or_id: str | int) -> Product:
        text = str(name_or_id).strip()
        # Name wins over id so a product literally called "2024" stays reachable.
        row = self._conn.execute(
            "SELECT * FROM products WHERE name = ? COLLATE NOCASE", (text,)
        ).fetchone()
        if row is not None:
            return self._row_to_product(row)
        if text.isdigit():
            return self.get_product(int(text))
        raise TrackerError(f"No product named {text!r}.")

    def add_order(
        self,
        *,
        purchaser: str,
        quantity: object,
        product: str | int | None = None,
    ) -> Order:
        products = self.list_products()
        if not products:
            raise TrackerError(
                "Establish a product first, then log who bought it."
            )
        if product is None:
            if len(products) > 1:
                raise TrackerError("Choose which product this order is for.")
            chosen = products[0]
        else:
            chosen = self.find_product(product)
        buyer = purchaser.strip()
        if not buyer:
            raise TrackerError("purchaser name is required.")
        ordered = parse_quantity(quantity)
        stamp = _now()
        cursor = self._conn.execute(
            """
            INSERT INTO orders (
                product_id, purchaser, quantity_ordered, quantity_received,
                created_at, updated_at
            )
            VALUES (?, ?, ?, '0', ?, ?)
            """,
            (chosen.id, buyer, str(ordered), stamp, stamp),
        )
        self._conn.commit()
        return self.get_order(cursor.lastrowid)

    def get_order(self, order_id: int) -> Order:
        row = self._conn.execute(self._order_sql("WHERE o.id = ?"), (order_id,)).fetchone()
        if row is None:
            raise TrackerError(f"No order with id {order_id}.")
        return self._row_to_order(row)

    def list_orders(
        self,
        *,
        search: str | None = None,
        status: str = "all",
    ) -> list[Order]:
        clauses: list[str] = []
        params: list[object] = []
        if search:
            # Escape LIKE metacharacters so a typed % or _ is matched literally.
            literal = (
                search.strip()
                .replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            needle = f"%{literal}%"
            clauses.append(
                "(o.purchaser LIKE ? ESCAPE '\\' COLLATE NOCASE "
                "OR p.name LIKE ? ESCAPE '\\' COLLATE NOCASE)"
            )
            params.extend([needle, needle])
        flag = (status or "all").strip().lower()
        if flag not in {"all", "outstanding", "received"}:
            raise TrackerError("status must be all, outstanding, or received.")
        if flag == "outstanding":
            clauses.append(
                "CAST(o.quantity_received AS REAL) < CAST(o.quantity_ordered AS REAL)"
            )
        elif flag == "received":
            clauses.append(
                "CAST(o.quantity_received AS REAL) >= CAST(o.quantity_ordered AS REAL)"
            )
        sql = self._order_sql()
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY o.id"
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_order(row) for row in rows]

    def set_received(self, order_id: int, quantity: object) -> Order:
        order = self.get_order(order_id)
        received = parse_qty(
            quantity,
            field="received",
            minimum=Decimal("0"),
            maximum=order.quantity_ordered,
        )
        self._conn.execute(
            """
            UPDATE orders
            SET quantity_received = ?, updated_at = ?
            WHERE id = ?
            """,
            (str(received), _now(), order_id),
        )
        self._conn.commit()
        return self.get_order(order_id)

    def mark_received(self, order_id: int) -> Order:
        return self.set_received(order_id, self.get_order(order_id).quantity_ordered)

    def summary(self) -> Summary:
        orders = self.list_orders()
        outstanding = [order for order in orders if not order.fulfilled]
        received = [order for order in orders if order.fulfilled]
        units_ordered = sum((o.quantity_ordered for o in orders), Decimal("0"))
        units_received = sum((o.quantity_received for o in orders), Decimal("0"))
        units_remaining = sum((o.remaining for o in orders), Decimal("0"))
        revenue = sum((o.total for o in orders), Decimal("0.00"))
        return Summary(
            order_count=len(orders),
            outstanding_count=len(outstanding),
            received_count=len(received),
            units_ordered=units_ordered,
            units_received=units_received,
            units_remaining=units_remaining,
            revenue=revenue.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP),
        )

    def delete_order(self, order_id: int) -> Order:
        """Remove one order. Settings-only; the main list has no delete."""
        order = self.get_order(order_id)
        self._conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))
        self._conn.commit()
        return order

    def count_orders_for_product(self, product_id: int) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM orders WHERE product_id = ?", (product_id,)
        ).fetchone()
        return int(row[0])

    def delete_product(self, product_id: int) -> Product:
        """Remove one product. Refuses while orders still reference it."""
        product = self.get_product(product_id)
        attached = self.count_orders_for_product(product_id)
        if attached:
            raise TrackerError(
                f"{product.name} still has {attached} order(s). "
                "Delete those orders first."
            )
        self._conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
        self._conn.commit()
        return product

    def reset_orders(self) -> int:
        count = self._conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        self._conn.execute("DELETE FROM orders")
        self._conn.execute("DELETE FROM sqlite_sequence WHERE name = 'orders'")
        self._conn.commit()
        return int(count)

    def reset_all(self) -> None:
        self._conn.execute("DELETE FROM orders")
        self._conn.execute("DELETE FROM products")
        self._conn.execute(
            "DELETE FROM sqlite_sequence WHERE name IN ('orders', 'products')"
        )
        self._conn.commit()

    def _validated_product(
        self,
        *,
        name: str,
        unit: str,
        unit_price: object,
        sku: str,
        notes: str,
    ) -> dict[str, object]:
        clean_name = name.strip()
        clean_unit = unit.strip().lower() or "each"
        if not clean_name:
            raise TrackerError("product name is required.")
        if len(clean_name) > 80:
            raise TrackerError("product name is too long.")
        if len(clean_unit) > 24:
            raise TrackerError("unit is too long.")
        return {
            "name": clean_name,
            "unit": clean_unit,
            "unit_price": parse_money(unit_price, field="price"),
            "sku": sku.strip(),
            "notes": notes.strip(),
        }

    @staticmethod
    def _order_sql(where: str = "") -> str:
        return f"""
            SELECT
                o.id, o.product_id, o.purchaser, o.quantity_ordered,
                o.quantity_received, o.created_at, o.updated_at,
                p.name AS product_name, p.unit AS product_unit,
                p.unit_price AS unit_price
            FROM orders o
            JOIN products p ON p.id = o.product_id
            {where}
        """

    @staticmethod
    def _row_to_product(row: sqlite3.Row) -> Product:
        return Product(
            id=row["id"],
            name=row["name"],
            unit=row["unit"],
            unit_price=Decimal(row["unit_price"]),
            sku=row["sku"],
            notes=row["notes"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _row_to_order(row: sqlite3.Row) -> Order:
        return Order(
            id=row["id"],
            product_id=row["product_id"],
            product_name=row["product_name"],
            product_unit=row["product_unit"],
            unit_price=Decimal(row["unit_price"]),
            purchaser=row["purchaser"],
            quantity_ordered=Decimal(row["quantity_ordered"]),
            quantity_received=Decimal(row["quantity_received"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

