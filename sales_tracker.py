#!/usr/bin/env python3
"""Sales Tracker — establish a product, log orders, track what has been handed out.

Orders stay on the list until the operator resets them from Settings.
The GUI in gui.py uses the same SalesTracker class.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Callable, TextIO


def application_dir() -> Path:
    """Directory that holds sales.db — next to the .exe when frozen."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


DEFAULT_DB = application_dir() / "sales.db"
MONEY_QUANT = Decimal("0.01")
QTY_QUANT = Decimal("0.001")
UNITS = ("each", "jar", "box", "dozen", "lb", "bag", "case", "pack", "bottle")
SCHEMA_VERSION = 1


class TrackerError(ValueError):
    """User-facing validation or lookup error."""


def parse_money(value: object, *, field: str = "price") -> Decimal:
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError) as exc:
        raise TrackerError(f"{field} must be a number.") from exc
    if not amount.is_finite():
        raise TrackerError(f"{field} must be a real number.")
    if amount < 0:
        raise TrackerError(f"{field} cannot be negative.")
    try:
        return amount.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise TrackerError(f"{field} is too large.") from exc


def parse_qty(
    value: object,
    *,
    field: str = "quantity",
    minimum: Decimal = Decimal("0"),
    maximum: Decimal | None = None,
) -> Decimal:
    try:
        qty = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError) as exc:
        raise TrackerError(f"{field} must be a number.") from exc
    if not qty.is_finite():
        raise TrackerError(f"{field} must be a real number.")
    if qty < minimum:
        if minimum == 0:
            raise TrackerError(f"{field} cannot be negative.")
        raise TrackerError(f"{field} must be greater than zero.")
    if maximum is not None and qty > maximum:
        raise TrackerError(
            f"{field} cannot be more than {format_qty(maximum)}."
        )
    try:
        return qty.quantize(QTY_QUANT, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise TrackerError(f"{field} is too large.") from exc


def parse_quantity(value: object) -> Decimal:
    return parse_qty(value, field="quantity", minimum=Decimal("0.001"))


def format_money(amount: Decimal) -> str:
    return f"${amount:,.2f}"


def format_qty(quantity: Decimal) -> str:
    text = format(quantity, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass(frozen=True)
class Product:
    id: int
    name: str
    unit: str
    unit_price: Decimal
    sku: str
    notes: str
    created_at: str


@dataclass(frozen=True)
class Order:
    id: int
    product_id: int
    product_name: str
    product_unit: str
    unit_price: Decimal
    purchaser: str
    quantity_ordered: Decimal
    quantity_received: Decimal
    created_at: str
    updated_at: str

    @property
    def remaining(self) -> Decimal:
        leftover = self.quantity_ordered - self.quantity_received
        return leftover if leftover > 0 else Decimal("0")

    @property
    def fulfilled(self) -> bool:
        return self.quantity_received >= self.quantity_ordered

    @property
    def status(self) -> str:
        return "received" if self.fulfilled else "outstanding"

    @property
    def total(self) -> Decimal:
        return (self.quantity_ordered * self.unit_price).quantize(
            MONEY_QUANT, rounding=ROUND_HALF_UP
        )


@dataclass(frozen=True)
class Summary:
    order_count: int
    outstanding_count: int
    received_count: int
    units_ordered: Decimal
    units_received: Decimal
    units_remaining: Decimal
    revenue: Decimal


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


PromptFn = Callable[[str], str]
WriteFn = Callable[[str], None]


def collect_product_answers(ask: PromptFn, write: WriteFn) -> dict[str, str]:
    """Walk through establishing a product, one question at a time."""
    write("Let's set up what you sell. One question at a time.\n")
    name = ask("What are you selling?\n> ").strip()
    write(
        "How do you count it? Common choices: "
        + ", ".join(UNITS)
        + ".\n"
    )
    unit = ask("> ").strip() or "each"
    price = ask(f"Price per {unit or 'unit'}? (0 if you are not tracking money)\n> ")
    write("Stock code or SKU? Press Enter to skip.\n")
    sku = ask("> ")
    write("Anything else to remember about it? Press Enter to skip.\n")
    notes = ask("> ")
    write("\nReview:\n")
    write(f"  Product : {name.strip() or '(missing)'}\n")
    write(f"  Unit    : {unit.strip() or 'each'}\n")
    write(f"  Price   : {price.strip() or '0'}\n")
    if sku.strip():
        write(f"  SKU     : {sku.strip()}\n")
    if notes.strip():
        write(f"  Notes   : {notes.strip()}\n")
    confirm = ask("Save this product? (yes / no)\n> ").strip().casefold()
    if confirm not in {"y", "yes"}:
        raise TrackerError("Product setup cancelled.")
    return {
        "name": name,
        "unit": unit,
        "unit_price": price,
        "sku": sku,
        "notes": notes,
    }


def _print_orders(orders: list[Order], write: WriteFn = sys.stdout.write) -> None:
    if not orders:
        write("No orders on the list.\n")
        return
    header = (
        f"{'#':>4}  {'Purchaser':<18}  {'Product':<16}  "
        f"{'Received':>16}  {'Status':<12}"
    )
    write(header + "\n")
    write("-" * len(header) + "\n")
    for order in orders:
        received = f"{format_qty(order.quantity_received)} / {format_qty(order.quantity_ordered)}"
        mark = "✓ received" if order.fulfilled else "outstanding"
        write(
            f"{order.id:>4}  {order.purchaser[:18]:<18}  "
            f"{order.product_name[:16]:<16}  {received:>16}  {mark:<12}\n"
        )


def _print_products(products: list[Product], write: WriteFn = sys.stdout.write) -> None:
    if not products:
        write("No product established yet.\n")
        return
    for product in products:
        extra = f"  sku {product.sku}" if product.sku else ""
        write(
            f"#{product.id}  {product.name}  "
            f"{format_money(product.unit_price)} / {product.unit}{extra}\n"
        )


def _print_summary(summary: Summary, write: WriteFn = sys.stdout.write) -> None:
    write(
        f"{summary.order_count} order(s)  ·  "
        f"{summary.outstanding_count} outstanding  ·  "
        f"{summary.received_count} received  ·  "
        f"{format_qty(summary.units_remaining)} still to hand out  ·  "
        f"{format_money(summary.revenue)} ordered\n"
    )


class InteractiveSession:
    """Menu-driven CLI used when the script is run with no subcommand."""

    def __init__(
        self,
        tracker: SalesTracker,
        stdin: TextIO,
        stdout: TextIO,
    ) -> None:
        self.tracker = tracker
        self.stdin = stdin
        self.stdout = stdout

    def ask(self, prompt: str) -> str:
        self.stdout.write(prompt)
        self.stdout.flush()
        line = self.stdin.readline()
        if line == "":
            raise EOFError
        return line.rstrip("\n")

    def write(self, text: str) -> None:
        self.stdout.write(text)

    def run(self) -> int:
        self.write("Sales Tracker — orders stay until you reset them in Settings.\n")
        if not self.tracker.list_products():
            self.write("\nNothing is set up to sell yet.\n")
            if not self._setup_product():
                return 0
        while True:
            try:
                choice = self._menu()
            except EOFError:
                self.write("\n")
                return 0
            if choice in {"0", "q", "quit", "exit"}:
                self.write("Bye.\n")
                return 0
            try:
                self._dispatch(choice)
            except TrackerError as exc:
                self.write(f"error: {exc}\n")
            except EOFError:
                self.write("\n")
                return 0

    def _menu(self) -> str:
        self.write("\nWhat next?\n")
        self.write("  1) Log an order (name + how many they bought)\n")
        self.write("  2) Update how many someone has received\n")
        self.write("  3) Show the list\n")
        self.write("  4) Establish another product\n")
        self.write("  5) Settings (reset)\n")
        self.write("  0) Quit\n")
        return self.ask("> ").strip().casefold()

    def _dispatch(self, choice: str) -> None:
        if choice in {"1", "log", "order"}:
            self._log_order()
        elif choice in {"2", "receive", "update"}:
            self._update_received()
        elif choice in {"3", "list"}:
            _print_orders(self.tracker.list_orders(), self.write)
            _print_summary(self.tracker.summary(), self.write)
        elif choice in {"4", "product"}:
            self._setup_product()
        elif choice in {"5", "settings"}:
            self._settings()
        else:
            self.write("Choose 1, 2, 3, 4, 5, or 0.\n")

    def _setup_product(self) -> bool:
        answers = collect_product_answers(self.ask, self.write)
        product = self.tracker.add_product(
            name=answers["name"],
            unit=answers["unit"],
            unit_price=answers["unit_price"],
            sku=answers["sku"],
            notes=answers["notes"],
        )
        self.write(
            f"Saved: {product.name} — {format_money(product.unit_price)} / {product.unit}.\n"
        )
        return True

    def _log_order(self) -> None:
        products = self.tracker.list_products()
        if not products:
            self.write("Establish a product first.\n")
            return
        product: Product | None = products[0]
        if len(products) > 1:
            _print_products(products, self.write)
            product = self.tracker.find_product(
                self.ask("Which product? (name or number)\n> ")
            )
        assert product is not None
        purchaser = self.ask("Purchaser name?\n> ")
        quantity = self.ask(
            f"How many {product.unit} did {purchaser.strip() or 'they'} buy?\n> "
        )
        order = self.tracker.add_order(
            purchaser=purchaser,
            quantity=quantity,
            product=product.id,
        )
        self.write(
            f"Logged #{order.id}: {order.purchaser} ordered "
            f"{format_qty(order.quantity_ordered)} {order.product_unit} of "
            f"{order.product_name} (0 received so far).\n"
        )

    def _update_received(self) -> None:
        orders = self.tracker.list_orders()
        if not orders:
            self.write("No orders yet.\n")
            return
        _print_orders(orders, self.write)
        raw_id = self.ask("Order number?\n> ").strip()
        try:
            order = self.tracker.get_order(int(raw_id))
        except ValueError as exc:
            raise TrackerError("Order number must be a whole number.") from exc
        got = self.ask(
            f"{order.purchaser} needs {format_qty(order.quantity_ordered)} "
            f"{order.product_unit}. How many have they received so far?\n> "
        )
        updated = self.tracker.set_received(order.id, got)
        self.write(
            f"Updated: {updated.purchaser}  "
            f"{format_qty(updated.quantity_received)} / "
            f"{format_qty(updated.quantity_ordered)}"
            f"  ({updated.status}).\n"
        )

    def _delete_order(self) -> None:
        orders = self.tracker.list_orders()
        if not orders:
            self.write("No orders to delete.\n")
            return
        _print_orders(orders, self.write)
        raw_id = self.ask("Delete which order number?\n> ").strip()
        try:
            order = self.tracker.get_order(int(raw_id))
        except ValueError as exc:
            raise TrackerError("Order number must be a whole number.") from exc
        self.write(
            f"This deletes {order.purchaser}'s order for "
            f"{format_qty(order.quantity_ordered)} {order.product_unit} of "
            f"{order.product_name}. It cannot be undone.\n"
        )
        if self.ask("Type DELETE to confirm.\n> ").strip() != "DELETE":
            self.write("Delete cancelled.\n")
            return
        self.tracker.delete_order(order.id)
        self.write(f"Deleted order #{order.id}.\n")

    def _delete_product(self) -> None:
        products = self.tracker.list_products()
        if not products:
            self.write("No products to delete.\n")
            return
        _print_products(products, self.write)
        product = self.tracker.find_product(
            self.ask("Delete which product? (name or number)\n> ")
        )
        attached = self.tracker.count_orders_for_product(product.id)
        if attached:
            raise TrackerError(
                f"{product.name} still has {attached} order(s). "
                "Delete those orders first."
            )
        self.write(f"This deletes {product.name}. It cannot be undone.\n")
        if self.ask("Type DELETE to confirm.\n> ").strip() != "DELETE":
            self.write("Delete cancelled.\n")
            return
        self.tracker.delete_product(product.id)
        self.write(f"Deleted {product.name}.\n")

    def _settings(self) -> None:
        self.write("\nSettings\n")
        self.write("  Orders never disappear unless you remove them here.\n")
        self.write("  o) Delete one order\n")
        self.write("  p) Delete one product\n")
        self.write("  r) Reset all orders (keeps products)\n")
        self.write("  a) Reset everything (products and orders)\n")
        self.write("  b) Back\n")
        choice = self.ask("> ").strip().casefold()
        if choice == "o":
            self._delete_order()
        elif choice == "p":
            self._delete_product()
        elif choice == "r":
            confirm = self.ask("Type RESET to clear every order off the list.\n> ")
            if confirm.strip() != "RESET":
                self.write("Reset cancelled.\n")
                return
            count = self.tracker.reset_orders()
            self.write(f"Cleared {count} order(s). Products are still on file.\n")
        elif choice == "a":
            confirm = self.ask("Type RESET to wipe products and orders.\n> ")
            if confirm.strip() != "RESET":
                self.write("Reset cancelled.\n")
                return
            self.tracker.reset_all()
            self.write("Everything cleared.\n")
        else:
            self.write("Back.\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Establish a product, log orders, and track what has been handed out.",
        epilog="With no command, starts an interactive session. Desktop UI: python gui.py",
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB),
        help="Path to the SQLite database (default: sales.db next to this script).",
    )
    sub = parser.add_subparsers(dest="command")

    product = sub.add_parser("product", help="Establish a product (interactive if flags omitted)")
    product.add_argument("--name")
    product.add_argument("--unit", default="each")
    product.add_argument("--price", default="0")
    product.add_argument("--sku", default="")
    product.add_argument("--notes", default="")

    sub.add_parser("products", help="List products")

    order = sub.add_parser("order", help="Log a purchaser and how many they bought")
    order.add_argument("--buyer", required=True, help="Purchaser name")
    order.add_argument("--qty", required=True, help="Quantity ordered")
    order.add_argument("--product", help="Product name or id (needed if more than one)")

    listed = sub.add_parser("list", help="Show the order list")
    listed.add_argument("--search", help="Match purchaser or product")
    listed.add_argument(
        "--status",
        choices=("all", "outstanding", "received"),
        default="all",
    )

    receive = sub.add_parser("receive", help="Set how many of an order have been handed out")
    receive.add_argument("id", type=int)
    receive.add_argument("--got", help="Number received so far")
    receive.add_argument("--all", dest="all_of_it", action="store_true", help="Mark fully received")

    sub.add_parser("summary", help="Print outstanding vs received totals")

    reset = sub.add_parser("reset", help="Clear the list (the only way orders disappear)")
    reset.add_argument("--orders", action="store_true", help="Delete all orders, keep products")
    reset.add_argument("--all", dest="all_data", action="store_true", help="Delete products and orders")
    reset.add_argument("--yes", action="store_true", help="Confirm the reset")

    delete = sub.add_parser(
        "delete", help="Delete one order or product (Settings-level action)"
    )
    delete.add_argument("kind", choices=("order", "product"))
    delete.add_argument("id", type=int)
    delete.add_argument("--yes", action="store_true", help="Confirm the delete")

    sub.add_parser("interactive", help="Menu-driven session")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None or args.command == "interactive":
        with SalesTracker(args.db) as tracker:
            return InteractiveSession(tracker, sys.stdin, sys.stdout).run()

    try:
        with SalesTracker(args.db) as tracker:
            if args.command == "product":
                if args.name:
                    product = tracker.add_product(
                        name=args.name,
                        unit=args.unit,
                        unit_price=args.price,
                        sku=args.sku,
                        notes=args.notes,
                    )
                else:
                    answers = collect_product_answers(lambda p: input(p), sys.stdout.write)
                    product = tracker.add_product(
                        name=answers["name"],
                        unit=answers["unit"],
                        unit_price=answers["unit_price"],
                        sku=answers["sku"],
                        notes=answers["notes"],
                    )
                print(
                    f"Saved product #{product.id}: {product.name} "
                    f"({format_money(product.unit_price)} / {product.unit})"
                )
            elif args.command == "products":
                _print_products(tracker.list_products())
            elif args.command == "order":
                order = tracker.add_order(
                    purchaser=args.buyer,
                    quantity=args.qty,
                    product=args.product,
                )
                print(
                    f"Logged #{order.id}: {order.purchaser} ordered "
                    f"{format_qty(order.quantity_ordered)} {order.product_unit} of "
                    f"{order.product_name}"
                )
            elif args.command == "list":
                _print_orders(
                    tracker.list_orders(search=args.search, status=args.status)
                )
            elif args.command == "receive":
                if args.all_of_it:
                    order = tracker.mark_received(args.id)
                elif args.got is None:
                    raise TrackerError("Pass --got N or --all.")
                else:
                    order = tracker.set_received(args.id, args.got)
                print(
                    f"{order.purchaser}: {format_qty(order.quantity_received)} / "
                    f"{format_qty(order.quantity_ordered)} {order.product_unit} "
                    f"({order.status})"
                )
            elif args.command == "summary":
                _print_summary(tracker.summary())
            elif args.command == "delete":
                if not args.yes:
                    raise TrackerError("Delete refused: pass --yes to confirm.")
                if args.kind == "order":
                    removed = tracker.delete_order(args.id)
                    print(
                        f"Deleted order #{removed.id} "
                        f"({removed.purchaser} / {removed.product_name})."
                    )
                else:
                    product = tracker.delete_product(args.id)
                    print(f"Deleted product #{product.id} ({product.name}).")
            elif args.command == "reset":
                if not args.yes:
                    raise TrackerError("Reset refused: pass --yes to confirm.")
                if args.all_data:
                    tracker.reset_all()
                    print("Reset everything.")
                elif args.orders:
                    count = tracker.reset_orders()
                    print(f"Reset {count} order(s). Products kept.")
                else:
                    raise TrackerError("Pass --orders or --all.")
    except TrackerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
