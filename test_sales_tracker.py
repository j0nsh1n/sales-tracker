#!/usr/bin/env python3
"""Tests for the Sales Tracker core library, CLI, and GUI."""

from __future__ import annotations

import ast
import csv
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime
from decimal import Decimal
from functools import partial
from pathlib import Path
from unittest.mock import patch

from salestracker.ui import theme
from salestracker.finance import (
    DENOMINATIONS,
    count_cash,
    reconcile,
)
from sales_tracker import (
    CASH,
    PAYMENT_METHODS,
    SCHEMA_VERSION,
    InteractiveSession,
    SalesTracker,
    TrackerError,
    application_dir,
    collect_product_answers,
    format_money,
    format_payment_method,
    main,
    parse_money,
    parse_payment_method,
    parse_quantity,
)


class SalesTrackerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        # Registered before any test opens a connection of its own, so it runs
        # last: cleanups run after tearDown and in reverse order, and Windows
        # refuses to unlink a database file while a connection to it is open.
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "sales.db"
        self.tracker = SalesTracker(self.db)

    def tearDown(self) -> None:
        # Looked up dynamically: some tests replace self.tracker mid-test.
        self.tracker.close()

    def _product(self, name: str = "Honey", **kwargs):
        payload = dict(name=name, unit="jar", unit_price="12.50")
        payload.update(kwargs)
        return self.tracker.add_product(**payload)

    def _user_version(self, path: Path | None = None) -> int:
        conn = sqlite3.connect(path or self.db)
        try:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])
        finally:
            conn.close()

    def test_cannot_order_without_product(self) -> None:
        with self.assertRaises(TrackerError):
            self.tracker.add_order(purchaser="Jim", quantity="10")

    def test_product_then_order(self) -> None:
        self._product()
        order = self.tracker.add_order(purchaser="Jim", quantity="10")
        self.assertEqual(order.purchaser, "Jim")
        self.assertEqual(order.quantity_ordered, Decimal("10"))
        self.assertEqual(order.quantity_received, Decimal("0"))
        self.assertFalse(order.fulfilled)
        self.assertEqual(order.remaining, Decimal("10"))

    def test_rejects_blank_product_and_duplicate(self) -> None:
        with self.assertRaises(TrackerError):
            self._product(name="  ")
        self._product("Honey")
        with self.assertRaises(TrackerError):
            self._product("honey")

    def test_partial_received_stays_on_list(self) -> None:
        self._product()
        order = self.tracker.add_order(purchaser="Jim", quantity="10")
        updated = self.tracker.set_received(order.id, "5")
        self.assertEqual(updated.quantity_received, Decimal("5"))
        self.assertEqual(updated.remaining, Decimal("5"))
        self.assertFalse(updated.fulfilled)
        listed = self.tracker.list_orders()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].purchaser, "Jim")

    def test_full_received_stays_on_list(self) -> None:
        self._product()
        order = self.tracker.add_order(purchaser="Jim", quantity="10")
        updated = self.tracker.mark_received(order.id)
        self.assertTrue(updated.fulfilled)
        self.assertEqual(len(self.tracker.list_orders()), 1)
        self.assertEqual(len(self.tracker.list_orders(status="received")), 1)
        self.assertEqual(len(self.tracker.list_orders(status="outstanding")), 0)

    def test_received_cannot_exceed_ordered(self) -> None:
        self._product()
        order = self.tracker.add_order(purchaser="Jim", quantity="10")
        with self.assertRaises(TrackerError):
            self.tracker.set_received(order.id, "11")

    def test_received_cannot_be_negative(self) -> None:
        self._product()
        order = self.tracker.add_order(purchaser="Ada", quantity="2")
        with self.assertRaises(TrackerError):
            self.tracker.set_received(order.id, "-1")

    def test_delete_order_removes_only_that_row(self) -> None:
        self._product()
        keep = self.tracker.add_order(purchaser="Jim", quantity="10")
        drop = self.tracker.add_order(purchaser="Ann", quantity="4")
        removed = self.tracker.delete_order(drop.id)
        self.assertEqual(removed.purchaser, "Ann")
        self.assertEqual([o.id for o in self.tracker.list_orders()], [keep.id])
        self.assertEqual(len(self.tracker.list_products()), 1)
        with self.assertRaises(TrackerError):
            self.tracker.get_order(drop.id)

    def test_delete_order_rejects_unknown_id(self) -> None:
        with self.assertRaises(TrackerError):
            self.tracker.delete_order(999)

    def test_delete_product_blocked_while_orders_attached(self) -> None:
        product = self._product()
        self.tracker.add_order(purchaser="Jim", quantity="10")
        self.tracker.add_order(purchaser="Ann", quantity="2")
        with self.assertRaises(TrackerError) as ctx:
            self.tracker.delete_product(product.id)
        self.assertIn("2 order(s)", str(ctx.exception))
        self.assertEqual(len(self.tracker.list_products()), 1)
        self.assertEqual(len(self.tracker.list_orders()), 2)

    def test_delete_product_allowed_once_orders_are_gone(self) -> None:
        product = self._product()
        order = self.tracker.add_order(purchaser="Jim", quantity="10")
        self.tracker.delete_order(order.id)
        self.assertEqual(self.tracker.count_orders_for_product(product.id), 0)
        removed = self.tracker.delete_product(product.id)
        self.assertEqual(removed.name, "Honey")
        self.assertEqual(self.tracker.list_products(), [])

    def test_deletes_survive_reopen(self) -> None:
        self._product()
        order = self.tracker.add_order(purchaser="Jim", quantity="10")
        self.tracker.delete_order(order.id)
        self.tracker.close()
        with SalesTracker(self.db) as reopened:
            self.assertEqual(reopened.list_orders(), [])
        self.tracker = SalesTracker(self.db)

    def test_reset_orders_keeps_products(self) -> None:
        self._product()
        self.tracker.add_order(purchaser="Jim", quantity="10")
        self.tracker.add_order(purchaser="Ada", quantity="3")
        count = self.tracker.reset_orders()
        self.assertEqual(count, 2)
        self.assertEqual(self.tracker.list_orders(), [])
        self.assertEqual(len(self.tracker.list_products()), 1)

    def test_setting_round_trips_and_survives_reopen(self) -> None:
        self.assertEqual(self.tracker.get_setting("theme", "system"), "system")
        self.tracker.set_setting("theme", "dark")
        self.assertEqual(self.tracker.get_setting("theme"), "dark")
        self.tracker.set_setting("theme", "light")  # upsert, not a second row
        self.assertEqual(self.tracker.get_setting("theme"), "light")
        self.tracker.close()
        with SalesTracker(self.db) as reopened:
            self.assertEqual(reopened.get_setting("theme"), "light")
        self.tracker = SalesTracker(self.db)

    def test_settings_are_not_ledger_data_and_survive_reset_all(self) -> None:
        # "Reset everything" is about products and orders; wiping the
        # operator's preferences is not something clearing the ledger implies.
        self._product()
        self.tracker.add_order(purchaser="Jim", quantity="10")
        self.tracker.set_setting("theme", "dark")
        self.tracker.reset_all()
        self.assertEqual(self.tracker.get_setting("theme"), "dark")

    def test_reset_all(self) -> None:
        self._product()
        self.tracker.add_order(purchaser="Jim", quantity="10")
        self.tracker.reset_all()
        self.assertEqual(self.tracker.list_orders(), [])
        self.assertEqual(self.tracker.list_products(), [])

    def test_search_and_status_filter(self) -> None:
        self._product()
        jim = self.tracker.add_order(purchaser="Jim", quantity="10")
        self.tracker.add_order(purchaser="Ada", quantity="4")
        self.tracker.mark_received(jim.id)
        found = self.tracker.list_orders(search="ada")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].purchaser, "Ada")
        self.assertEqual(len(self.tracker.list_orders(status="outstanding")), 1)

    def test_summary(self) -> None:
        self._product()
        first = self.tracker.add_order(purchaser="Jim", quantity="10")
        self.tracker.add_order(purchaser="Ada", quantity="2")
        self.tracker.set_received(first.id, "4")
        summary = self.tracker.summary()
        self.assertEqual(summary.order_count, 2)
        self.assertEqual(summary.outstanding_count, 2)
        self.assertEqual(summary.units_remaining, Decimal("8"))
        self.assertEqual(summary.revenue, Decimal("150.00"))

    def test_persists_received(self) -> None:
        self._product()
        order = self.tracker.add_order(purchaser="Jim", quantity="10")
        self.tracker.set_received(order.id, "5")
        self.tracker.close()
        with SalesTracker(self.db) as reopened:
            listed = reopened.list_orders()
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0].quantity_received, Decimal("5"))

    def test_multiple_products_require_choice(self) -> None:
        self._product("Honey")
        self._product("Soap", unit="bar", unit_price="4")
        with self.assertRaises(TrackerError):
            self.tracker.add_order(purchaser="Jim", quantity="1")
        order = self.tracker.add_order(purchaser="Jim", quantity="2", product="Soap")
        self.assertEqual(order.product_name, "Soap")

    def test_format_money(self) -> None:
        self.assertEqual(format_money(Decimal("1240")), "$1,240.00")

    def test_application_dir_is_script_folder_when_not_frozen(self) -> None:
        import sales_tracker as mod

        self.assertEqual(
            application_dir(), Path(mod.__file__).resolve().parent
        )

    def test_legacy_sales_table_migrates(self) -> None:
        self.tracker.close()
        raw = sqlite3.connect(self.db)
        raw.execute("DROP TABLE IF EXISTS orders")
        raw.execute("DROP TABLE IF EXISTS products")
        raw.execute(
            """
            CREATE TABLE sales (
                id INTEGER PRIMARY KEY,
                date TEXT, customer TEXT, item TEXT,
                quantity TEXT, unit_price TEXT, notes TEXT, created_at TEXT
            )
            """
        )
        raw.execute(
            """
            INSERT INTO sales VALUES
            (1, '2026-08-01', 'Ada', 'Notebook', '2', '12.50', '', '2026-08-01T10:00:00')
            """
        )
        raw.execute("PRAGMA user_version = 0")
        raw.commit()
        raw.close()
        with SalesTracker(self.db) as migrated:
            products = migrated.list_products()
            orders = migrated.list_orders()
            self.assertEqual(len(products), 1)
            self.assertEqual(products[0].name, "Notebook")
            self.assertEqual(len(orders), 1)
            self.assertEqual(orders[0].purchaser, "Ada")
            self.assertEqual(orders[0].quantity_received, Decimal("0"))
            self.assertEqual(self._user_version(), SCHEMA_VERSION)

    def test_rejects_non_finite_and_oversized_numbers(self) -> None:
        for bad in ("nan", "snan", "Infinity", "-Infinity", "1e999"):
            with self.subTest(value=bad):
                with self.assertRaises(TrackerError):
                    parse_money(bad)
                with self.assertRaises(TrackerError):
                    parse_quantity(bad)

    def test_order_rejects_non_finite_quantity(self) -> None:
        self._product()
        for bad in ("nan", "Infinity", "1e999"):
            with self.subTest(value=bad):
                with self.assertRaises(TrackerError):
                    self.tracker.add_order(purchaser="Jim", quantity=bad)
        self.assertEqual(self.tracker.list_orders(), [])

    def test_product_rejects_non_finite_price(self) -> None:
        for bad in ("nan", "Infinity", "1e999"):
            with self.subTest(value=bad):
                with self.assertRaises(TrackerError):
                    self.tracker.add_product(name=f"P{bad}", unit_price=bad)
        self.assertEqual(self.tracker.list_products(), [])

    def test_search_treats_like_wildcards_literally(self) -> None:
        self._product()
        self.tracker.add_order(purchaser="Jim", quantity="1")
        self.tracker.add_order(purchaser="100% Ann", quantity="1")
        self.assertEqual(
            [o.purchaser for o in self.tracker.list_orders(search="%")],
            ["100% Ann"],
        )
        self.assertEqual(self.tracker.list_orders(search="_"), [])
        self.assertEqual(
            [o.purchaser for o in self.tracker.list_orders(search="Ji")], ["Jim"]
        )

    def test_all_digit_product_name_is_reachable(self) -> None:
        self._product()
        self.tracker.add_product(name="2024", unit="case", unit_price="5")
        self.assertEqual(self.tracker.find_product("2024").name, "2024")
        self.assertEqual(self.tracker.find_product("1").name, "Honey")

    def test_interrupted_legacy_migration_can_retry(self) -> None:
        self.tracker.close()
        raw = sqlite3.connect(self.db)
        raw.execute("DROP TABLE IF EXISTS orders")
        raw.execute("DROP TABLE IF EXISTS products")
        raw.execute(
            """
            CREATE TABLE sales (
                id INTEGER PRIMARY KEY,
                date TEXT, customer TEXT, item TEXT,
                quantity TEXT, unit_price TEXT, notes TEXT, created_at TEXT
            )
            """
        )
        raw.executemany(
            "INSERT INTO sales VALUES (?,?,?,?,?,?,?,?)",
            [
                (1, "2026-08-01", "Ada", "Notebook", "2", "12.50", "", "2026-08-01T10:00:00"),
                (2, "2026-08-02", "Bo", "Pencil", "3", "1.50", "", "2026-08-02T10:00:00"),
            ],
        )
        raw.execute("PRAGMA user_version = 0")
        raw.commit()
        raw.close()

        real = SalesTracker._migration_product_id
        calls = []

        def flaky(self, name, unit_price):
            calls.append(name)
            if len(calls) == 2:
                raise RuntimeError("interrupted")
            return real(self, name, unit_price)

        with patch.object(SalesTracker, "_migration_product_id", flaky):
            with self.assertRaises(RuntimeError):
                SalesTracker(self.db)

        # Retrying must succeed and must not leave half-migrated duplicates.
        with SalesTracker(self.db) as retried:
            self.assertEqual(
                sorted(p.name for p in retried.list_products()),
                ["Notebook", "Pencil"],
            )
            self.assertEqual(
                sorted(o.purchaser for o in retried.list_orders()), ["Ada", "Bo"]
            )
            self.assertEqual(self._user_version(), SCHEMA_VERSION)

        self.tracker = SalesTracker(self.db)

    def test_fresh_database_is_at_current_schema_version(self) -> None:
        self.assertEqual(self._user_version(), SCHEMA_VERSION)
        self.assertGreaterEqual(SCHEMA_VERSION, 2)

    def test_reopen_at_current_version_is_a_noop(self) -> None:
        self._product()
        self.tracker.close()
        with patch.object(
            SalesTracker, "_migrate_to_v1", side_effect=AssertionError("ran")
        ):
            reopened = SalesTracker(self.db)
        self.addCleanup(reopened.close)
        self.assertEqual(self._user_version(), SCHEMA_VERSION)
        self.assertEqual(len(reopened.list_products()), 1)

    def test_refuses_newer_schema_than_code(self) -> None:
        self.tracker.close()
        raw = sqlite3.connect(self.db)
        raw.execute("PRAGMA user_version = 99")
        raw.commit()
        raw.close()
        with self.assertRaises(TrackerError) as ctx:
            SalesTracker(self.db)
        self.assertIn("99", str(ctx.exception))
        self.assertIn(str(SCHEMA_VERSION), str(ctx.exception))
        self.assertEqual(self._user_version(), 99)

    # ---------------------------------------------------------- payment split

    def test_orders_default_to_cash(self) -> None:
        self._product()
        order = self.tracker.add_order(purchaser="Jim", quantity="1")
        self.assertEqual(order.payment_method, CASH)
        self.assertTrue(order.is_cash)

    def test_rejects_unknown_payment_method(self) -> None:
        self._product()
        with self.assertRaises(TrackerError):
            self.tracker.add_order(
                purchaser="Jim", quantity="1", payment_method="bitcoin"
            )
        self.assertEqual(self.tracker.list_orders(), [])

    def test_payment_method_can_be_changed(self) -> None:
        self._product()
        order = self.tracker.add_order(purchaser="Jim", quantity="1")
        changed = self.tracker.set_payment_method(order.id, "venmo")
        self.assertEqual(changed.payment_method, "venmo")
        self.assertFalse(changed.is_cash)

    def test_edit_order_changes_only_what_is_passed(self) -> None:
        self._product()
        order = self.tracker.add_order(purchaser="Jim", quantity="10")
        self.tracker.set_received(order.id, "4")
        edited = self.tracker.edit_order(order.id, purchaser="  Jimmy ")
        self.assertEqual(edited.purchaser, "Jimmy")
        self.assertEqual(edited.quantity_ordered, Decimal("10"))
        self.assertEqual(edited.quantity_received, Decimal("4"))
        self.assertEqual(edited.payment_method, CASH)
        edited = self.tracker.edit_order(
            order.id, quantity="12", payment_method="Venmo"
        )
        self.assertEqual(edited.purchaser, "Jimmy")
        self.assertEqual(edited.quantity_ordered, Decimal("12"))
        self.assertEqual(edited.quantity_received, Decimal("4"))
        self.assertEqual(edited.payment_method, "venmo")

    def test_edit_order_cannot_drop_below_received(self) -> None:
        self._product()
        order = self.tracker.add_order(purchaser="Jim", quantity="10")
        self.tracker.set_received(order.id, "5")
        with self.assertRaisesRegex(TrackerError, "5 already received"):
            self.tracker.edit_order(order.id, purchaser="Ann", quantity="4")
        # Fails closed: the purchaser in the same call was not written either.
        unchanged = self.tracker.get_order(order.id)
        self.assertEqual(unchanged.purchaser, "Jim")
        self.assertEqual(unchanged.quantity_ordered, Decimal("10"))
        # Down to exactly what was received is allowed and completes the order.
        self.assertTrue(self.tracker.edit_order(order.id, quantity="5").fulfilled)

    def test_edit_order_rejects_bad_input(self) -> None:
        self._product()
        order = self.tracker.add_order(purchaser="Jim", quantity="10")
        for kwargs in (
            {"purchaser": "   "},
            {"quantity": "0"},
            {"quantity": "nan"},
            {"product": "Nope"},
            {"payment_method": "bitcoin"},
        ):
            with self.subTest(**kwargs), self.assertRaises(TrackerError):
                self.tracker.edit_order(order.id, **kwargs)
        self.assertEqual(self.tracker.get_order(order.id), order)
        with self.assertRaises(TrackerError):
            self.tracker.edit_order(999, purchaser="Ann")

    def test_edit_order_can_move_to_another_product(self) -> None:
        self._product()
        self._product("Jam", unit_price="4.00")
        order = self.tracker.add_order(purchaser="Jim", quantity="2", product="Honey")
        moved = self.tracker.edit_order(order.id, product="Jam")
        self.assertEqual(moved.product_name, "Jam")
        self.assertEqual(moved.total, Decimal("8.00"))

    def test_edit_product_changes_only_what_is_passed(self) -> None:
        product = self._product(sku="H-1", notes="raw")
        edited = self.tracker.edit_product(product.id, unit="Box", sku="")
        self.assertEqual(edited.name, "Honey")
        self.assertEqual(edited.unit, "box")
        self.assertEqual(edited.unit_price, Decimal("12.50"))
        self.assertEqual(edited.sku, "")
        self.assertEqual(edited.notes, "raw")
        self.assertEqual(edited.created_at, product.created_at)

    def test_edit_product_name_rules(self) -> None:
        honey = self._product()
        self._product("Jam")
        with self.assertRaisesRegex(TrackerError, "already on file"):
            self.tracker.edit_product(honey.id, name="JAM")
        with self.assertRaises(TrackerError):
            self.tracker.edit_product(honey.id, name="  ")
        with self.assertRaises(TrackerError):
            self.tracker.edit_product(honey.id, unit_price="-1")
        self.assertEqual(self.tracker.get_product(honey.id), honey)
        # Changing only the case of its own name is not a clash with itself.
        self.assertEqual(self.tracker.edit_product(honey.id, name="HONEY").name, "HONEY")

    def test_renamed_product_shows_on_its_orders(self) -> None:
        product = self._product()
        order = self.tracker.add_order(purchaser="Jim", quantity="2")
        self.tracker.edit_product(product.id, name="Wildflower honey")
        self.assertEqual(
            self.tracker.get_order(order.id).product_name, "Wildflower honey"
        )

    def test_price_change_warning(self) -> None:
        product = self._product()
        # No orders yet: nothing to reprice.
        self.assertEqual(self.tracker.price_change_warning(product.id, "14"), "")
        self.tracker.add_order(purchaser="Jim", quantity="2")
        self.assertEqual(self.tracker.price_change_warning(product.id, None), "")
        self.assertEqual(self.tracker.price_change_warning(product.id, "12.5"), "")
        warning = self.tracker.price_change_warning(product.id, "14")
        self.assertIn("1 order(s)", warning)
        self.assertIn("already collected", warning)
        with self.assertRaises(TrackerError):
            self.tracker.price_change_warning(product.id, "abc")

    def test_financials_split_cash_from_other(self) -> None:
        self._product()  # Honey, 12.50 / jar
        cash = self.tracker.add_order(purchaser="Jim", quantity="10")
        venmo = self.tracker.add_order(
            purchaser="Ann", quantity="4", payment_method="venmo"
        )
        self.tracker.add_order(
            purchaser="Bo", quantity="2", payment_method="other"
        )
        self.tracker.set_received(cash.id, "6")
        self.tracker.set_received(venmo.id, "4")

        money = self.tracker.financials()
        self.assertEqual(money.cash_collected, Decimal("75.00"))
        self.assertEqual(money.cash_uncollected, Decimal("50.00"))
        self.assertEqual(money.other_collected, Decimal("50.00"))
        self.assertEqual(money.other_uncollected, Decimal("25.00"))
        self.assertEqual(money.total_collected, Decimal("125.00"))
        # The money split must agree with the units-based summary.
        self.assertEqual(money.book_value, self.tracker.summary().revenue)

    def test_venmo_is_excluded_from_the_drawer(self) -> None:
        self._product()
        venmo = self.tracker.add_order(
            purchaser="Ann", quantity="4", payment_method="venmo"
        )
        self.tracker.set_received(venmo.id, "4")
        money = self.tracker.financials()
        self.assertEqual(money.cash_collected, Decimal("0.00"))
        self.assertEqual(money.other_collected, Decimal("50.00"))

    # ------------------------------------------------------------- cash count

    def test_count_cash_totals_denominations(self) -> None:
        self.assertEqual(count_cash({20: 3, 10: 1, 5: 1}), Decimal("75.00"))
        self.assertEqual(count_cash({100: 1, 2: 2}), Decimal("104.00"))
        self.assertEqual(count_cash({}), Decimal("0.00"))
        self.assertEqual(count_cash({d: 0 for d in DENOMINATIONS}), Decimal("0.00"))

    def test_count_cash_rejects_bad_counts(self) -> None:
        for bad in ("2.5", "-1", "abc", "nan", "Infinity"):
            with self.subTest(value=bad):
                with self.assertRaises(TrackerError):
                    count_cash({20: bad})

    def test_reconcile_reports_balanced_over_and_short(self) -> None:
        expected = Decimal("75.00")
        balanced = reconcile(expected, {20: 3, 10: 1, 5: 1})
        self.assertTrue(balanced.balanced)
        self.assertEqual(balanced.state, "balanced")
        self.assertEqual(balanced.difference, Decimal("0.00"))

        over = reconcile(expected, {20: 4})
        self.assertEqual(over.state, "over")
        self.assertEqual(over.difference, Decimal("5.00"))
        self.assertIn("$5.00", over.headline)

        short = reconcile(expected, {20: 3})
        self.assertEqual(short.state, "short")
        self.assertEqual(short.difference, Decimal("-15.00"))
        self.assertIn("$15.00", short.headline)

    # ----------------------------------------------------------------- export

    def test_export_csv_has_rows_and_totals(self) -> None:
        self._product()
        order = self.tracker.add_order(purchaser="Jim", quantity="10")
        venmo = self.tracker.add_order(
            purchaser="Ann", quantity="4", payment_method="venmo"
        )
        self.tracker.set_received(order.id, "6")
        self.tracker.set_received(venmo.id, "4")

        target = Path(self.tmp.name) / "out.csv"
        written = self.tracker.export_csv(target)
        self.assertTrue(written.exists())

        with written.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.reader(handle))

        self.assertEqual(rows[0][0], "id")
        self.assertIn("payment_method", rows[0])
        self.assertEqual(rows[1][1], "Jim")
        self.assertEqual(rows[1][rows[0].index("payment_method")], "cash")
        self.assertEqual(rows[2][rows[0].index("payment_method")], "venmo")

        flat = {r[0]: r[1] for r in rows if len(r) == 2}
        self.assertIn("TOTALS", [r[0] for r in rows if r])
        self.assertEqual(flat["Cash collected"], "75.00")
        self.assertEqual(flat["Other collected"], "50.00")
        self.assertEqual(flat["Orders"], "2")

    def test_export_csv_refuses_a_missing_folder(self) -> None:
        with self.assertRaises(TrackerError):
            self.tracker.export_csv(Path(self.tmp.name) / "nope" / "out.csv")


class WizardTests(unittest.TestCase):
    def test_collects_answers_and_can_cancel(self) -> None:
        lines = iter(["Honey", "jar", "12.50", "HNY", "wildflower", "yes"])
        out = io.StringIO()
        answers = collect_product_answers(lambda _p: next(lines), out.write)
        self.assertEqual(answers["name"], "Honey")
        self.assertEqual(answers["unit"], "jar")
        self.assertIn("Review", out.getvalue())

        lines = iter(["Honey", "jar", "12.50", "", "", "no"])
        with self.assertRaises(TrackerError):
            collect_product_answers(lambda _p: next(lines), io.StringIO().write)


class InteractiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "sales.db"
        self.tracker = SalesTracker(self.db)

    def tearDown(self) -> None:
        self.tracker.close()
        self.tmp.cleanup()

    def test_setup_log_receive_quit(self) -> None:
        script = "\n".join(
            [
                "Honey",
                "jar",
                "12.50",
                "",
                "",
                "yes",
                "1",
                "Jim",
                "10",
                "venmo",
                "2",
                "1",
                "5",
                "3",
                "0",
                "",
            ]
        )
        stdout = io.StringIO()
        session = InteractiveSession(self.tracker, io.StringIO(script), stdout)
        code = session.run()
        self.assertEqual(code, 0)
        orders = self.tracker.list_orders()
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0].payment_method, "venmo")
        self.assertEqual(orders[0].purchaser, "Jim")
        self.assertEqual(orders[0].quantity_received, Decimal("5"))
        self.assertIn("5 / 10", stdout.getvalue())

    def _run(self, *lines: str) -> str:
        stdout = io.StringIO()
        script = "\n".join([*lines, "0", ""])
        session = InteractiveSession(self.tracker, io.StringIO(script), stdout)
        self.assertEqual(session.run(), 0)
        return stdout.getvalue()

    def test_edit_order_keeps_blank_answers(self) -> None:
        self.tracker.add_product(name="Honey", unit="jar", unit_price="12.50")
        order = self.tracker.add_order(purchaser="Jim", quantity="10")
        # Only one product, so the product question is not asked.
        out = self._run("8", "o", str(order.id), "Jimmy", "", "venmo")
        edited = self.tracker.get_order(order.id)
        self.assertEqual(edited.purchaser, "Jimmy")
        self.assertEqual(edited.quantity_ordered, Decimal("10"))
        self.assertEqual(edited.payment_method, "venmo")
        self.assertIn("Updated #1", out)

    def test_edit_product_price_needs_confirming_and_dash_clears(self) -> None:
        self.tracker.add_product(
            name="Honey", unit="jar", unit_price="12.50", sku="H-1"
        )
        self.tracker.add_order(purchaser="Jim", quantity="10")
        out = self._run("8", "p", "Honey", "", "", "14", "-", "", "no")
        self.assertIn("including money already collected", out)
        self.assertIn("Nothing was changed", out)
        product = self.tracker.find_product("Honey")
        self.assertEqual(product.unit_price, Decimal("12.50"))
        self.assertEqual(product.sku, "H-1")

        self._run("8", "p", "Honey", "", "", "14", "-", "", "yes")
        product = self.tracker.find_product("Honey")
        self.assertEqual(product.unit_price, Decimal("14.00"))
        self.assertEqual(product.sku, "")


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "sales.db")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_product_order_receive_reset(self) -> None:
        self.assertEqual(
            main(
                [
                    "--db",
                    self.db,
                    "product",
                    "--name",
                    "Honey",
                    "--unit",
                    "jar",
                    "--price",
                    "12.50",
                ]
            ),
            0,
        )
        self.assertEqual(
            main(["--db", self.db, "order", "--buyer", "Jim", "--qty", "10"]),
            0,
        )
        self.assertEqual(main(["--db", self.db, "receive", "1", "--got", "5"]), 0)
        self.assertEqual(main(["--db", self.db, "list"]), 0)
        self.assertEqual(main(["--db", self.db, "summary"]), 0)
        self.assertEqual(main(["--db", self.db, "reset", "--orders"]), 1)
        self.assertEqual(main(["--db", self.db, "reset", "--orders", "--yes"]), 0)
        with SalesTracker(self.db) as tracker:
            self.assertEqual(tracker.list_orders(), [])
            self.assertEqual(len(tracker.list_products()), 1)

    def test_cli_delete_requires_yes_and_respects_attachment(self) -> None:
        with SalesTracker(self.db) as tracker:
            tracker.add_product(name="Honey", unit="jar", unit_price="12.50")
            order = tracker.add_order(purchaser="Jim", quantity="10")
        # Without --yes nothing is removed.
        self.assertEqual(main(["--db", self.db, "delete", "order", str(order.id)]), 1)
        with SalesTracker(self.db) as tracker:
            self.assertEqual(len(tracker.list_orders()), 1)
        # Product still carries an order, so it is refused.
        self.assertEqual(main(["--db", self.db, "delete", "product", "1", "--yes"]), 1)
        with SalesTracker(self.db) as tracker:
            self.assertEqual(len(tracker.list_products()), 1)
        # Delete the order, then the product goes.
        self.assertEqual(
            main(["--db", self.db, "delete", "order", str(order.id), "--yes"]), 0
        )
        self.assertEqual(main(["--db", self.db, "delete", "product", "1", "--yes"]), 0)
        with SalesTracker(self.db) as tracker:
            self.assertEqual(tracker.list_orders(), [])
            self.assertEqual(tracker.list_products(), [])

    def test_cli_rejects_order_without_product(self) -> None:
        code = main(["--db", self.db, "order", "--buyer", "Jim", "--qty", "2"])
        self.assertEqual(code, 1)

    def test_cli_edit_order(self) -> None:
        with SalesTracker(self.db) as tracker:
            tracker.add_product(name="Honey", unit="jar", unit_price="12.50")
            order = tracker.add_order(purchaser="Jim", quantity="10")
            tracker.set_received(order.id, "5")
        edit = ["--db", self.db, "edit", "order", str(order.id)]
        # Nothing to change, and a quantity under what was received, both fail.
        self.assertEqual(main(edit), 1)
        self.assertEqual(main([*edit, "--qty", "3"]), 1)
        self.assertEqual(
            main([*edit, "--buyer", "Jimmy", "--qty", "6", "--method", "venmo"]), 0
        )
        with SalesTracker(self.db) as tracker:
            edited = tracker.get_order(order.id)
        self.assertEqual(edited.purchaser, "Jimmy")
        self.assertEqual(edited.quantity_ordered, Decimal("6"))
        self.assertEqual(edited.quantity_received, Decimal("5"))
        self.assertEqual(edited.payment_method, "venmo")

    def test_cli_edit_product_price_needs_yes_once_orders_exist(self) -> None:
        with SalesTracker(self.db) as tracker:
            product = tracker.add_product(name="Honey", unit="jar", unit_price="12.50")
        edit = ["--db", self.db, "edit", "product", str(product.id)]
        self.assertEqual(main(edit), 1)
        # No orders yet, so a price change goes straight through.
        self.assertEqual(main([*edit, "--price", "13"]), 0)
        with SalesTracker(self.db) as tracker:
            tracker.add_order(purchaser="Jim", quantity="10")
        self.assertEqual(main([*edit, "--price", "14"]), 1)
        with SalesTracker(self.db) as tracker:
            self.assertEqual(tracker.get_product(product.id).unit_price, Decimal("13.00"))
        # Renaming does not touch the price, so it needs no --yes.
        self.assertEqual(main([*edit, "--name", "Wildflower"]), 0)
        self.assertEqual(main([*edit, "--price", "14", "--yes"]), 0)
        with SalesTracker(self.db) as tracker:
            edited = tracker.get_product(product.id)
        self.assertEqual(edited.name, "Wildflower")
        self.assertEqual(edited.unit_price, Decimal("14.00"))


def _tk_available() -> bool:
    """Tkinter needs a display; headless runners without one skip the GUI tests."""
    try:
        import tkinter

        root = tkinter.Tk()
    except Exception:
        return False
    root.destroy()
    return True


HAVE_TK = _tk_available()


@unittest.skipUnless(HAVE_TK, "no display available for tkinter")
class GuiSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "sales.db")
        from gui import SalesApp

        self.app = SalesApp(self.db, auto_setup=False)
        self.app.update_idletasks()

    def tearDown(self) -> None:
        self.app.tracker.close()
        self.app.destroy()
        self.tmp.cleanup()

    def test_log_and_partial_receive_row_remains(self) -> None:
        self.app.tracker.add_product(name="Honey", unit="jar", unit_price="12.50")
        self.app.refresh()
        self.app.update_idletasks()
        self.app.var_purchaser.set("Jim")
        self.app.var_qty.set("10")
        self.app.log_order()
        self.app.update_idletasks()

        rows = self.app.tree.get_children()
        self.assertEqual(len(rows), 1)
        self.app.tree.selection_set(rows[0])
        self.app._on_select()
        self.app.var_got.set("5")
        self.app.update_received()
        self.app.update_idletasks()

        self.assertEqual(len(self.app.tree.get_children()), 1)
        values = self.app.tree.item(rows[0], "values")
        self.assertEqual(values[1], "Jim")
        self.assertEqual(values[3], "10 jar")
        self.assertEqual(values[4], "5")
        self.assertEqual(values[7], "outstanding")

        self.app.var_got.set("10")
        self.app.update_received()
        self.app.update_idletasks()
        self.assertEqual(len(self.app.tree.get_children()), 1)
        values = self.app.tree.item(rows[0], "values")
        self.assertEqual(values[7], "received")

    def test_validation_without_purchaser(self) -> None:
        self.app.tracker.add_product(name="Honey", unit="jar", unit_price="12.50")
        self.app.refresh()
        self.app.var_purchaser.set("")
        self.app.var_qty.set("1")
        self.app.log_order()
        self.assertIn("purchaser", self.app.var_error.get())

    def test_settings_reset_clears_orders_only(self) -> None:
        self.app.tracker.add_product(name="Honey", unit="jar", unit_price="12.50")
        self.app.tracker.add_order(purchaser="Jim", quantity="10")
        self.app.refresh()
        from gui import SettingsDialog

        dialog = SettingsDialog(self.app, self.app.tracker, on_change=self.app.refresh)
        dialog.var_confirm.set("RESET")
        with patch("gui.messagebox.showinfo"):
            dialog._reset_orders()
        self.app.update_idletasks()
        self.assertEqual(self.app.tree.get_children(), ())
        self.assertEqual(len(self.app.tracker.list_products()), 1)

    def test_settings_deletes_are_gated_and_scoped(self) -> None:
        from gui import SettingsDialog

        tracker = self.app.tracker
        tracker.add_product(name="Honey", unit="jar", unit_price="12.50")
        tracker.add_product(name="Jam", unit="jar", unit_price="4.00")
        tracker.add_order(purchaser="Jim", quantity="10", product="Honey")
        drop = tracker.add_order(purchaser="Ann", quantity="4", product="Honey")
        self.app.refresh()

        dialog = SettingsDialog(self.app, tracker, on_change=self.app.refresh)
        dialog.update_idletasks()
        self.assertEqual(
            {dialog.products_tree.item(i, "values")[0] for i in dialog.products_tree.get_children()},
            {"Honey", "Jam"},
        )
        try:
            with patch("gui.messagebox.showinfo"), patch("gui.messagebox.showerror"):
                # Selected, but the RESET box is empty: nothing is removed.
                dialog.orders_tree.selection_set(str(drop.id))
                dialog._delete_order()
                self.assertEqual(len(tracker.list_orders()), 2)

                dialog.var_confirm.set("RESET")
                dialog._delete_order()
                self.assertEqual(
                    [o.purchaser for o in tracker.list_orders()], ["Jim"]
                )

                # Honey still carries Jim's order, so it is refused.
                honey = tracker.find_product("Honey")
                dialog.products_tree.selection_set(str(honey.id))
                dialog._delete_product()
                self.assertEqual(len(tracker.list_products()), 2)

                # Jam has no orders and goes.
                jam = tracker.find_product("Jam")
                dialog.products_tree.selection_set(str(jam.id))
                dialog._delete_product()
                self.assertEqual(
                    [p.name for p in tracker.list_products()], ["Honey"]
                )
        finally:
            dialog.destroy()

    def test_no_delete_control_on_main_window(self) -> None:
        import tkinter as tk

        texts: list[str] = []

        def walk(widget) -> None:
            try:
                texts.append(str(widget.cget("text")))
            except tk.TclError:
                pass
            for child in widget.winfo_children():
                walk(child)

        walk(self.app)
        joined = " ".join(texts).lower()
        self.assertNotIn("delete selected", joined)
        self.assertIn("settings", joined)


# A packaged windowed build (PyInstaller console=False) that is double-clicked
# has no console attached, so sys.stdout and sys.stderr are None. Anything the
# import chain evaluates at module level therefore has to survive that. These
# guards run on any platform, so Linux CI catches a Windows-only launch bug.
_STREAM_ATTRS = frozenset({"stdout", "stderr", "stdin"})

_PACKAGE_ROOT = Path(__file__).resolve().parent


def _import_time_nodes(tree: ast.AST):
    """Yield the AST nodes that run when the module is imported.

    Function bodies are skipped because they run later; decorators and default
    arguments are not, because those are evaluated at definition time -- which
    is exactly how sys.stdout.write once slipped into import-time code.
    """
    stack = list(getattr(tree, "body", []))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            stack.extend(getattr(node, "decorator_list", []))
            stack.extend(d for d in node.args.defaults if d is not None)
            stack.extend(d for d in node.args.kw_defaults if d is not None)
            continue
        stack.extend(ast.iter_child_nodes(node))


class FrozenLaunchGuardTests(unittest.TestCase):
    """Regressions for the packaged GUI failing to launch."""

    def test_no_import_time_console_stream_access(self) -> None:
        offenders = []
        for path in sorted(_PACKAGE_ROOT.glob("salestracker/**/*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in _import_time_nodes(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and node.attr in _STREAM_ATTRS
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "sys"
                ):
                    rel = path.relative_to(_PACKAGE_ROOT)
                    offenders.append(f"{rel}:{node.lineno} sys.{node.attr}")
        self.assertEqual(
            offenders,
            [],
            "sys.stdout/sys.stderr must not be read at import time; a "
            "double-clicked windowed build has them set to None. Resolve the "
            "stream inside the function instead.",
        )

    def test_gui_imports_without_console_streams(self) -> None:
        probe = textwrap.dedent(
            """
            import sys, traceback
            report = sys.argv[1]
            sys.stdout = None
            sys.stderr = None
            try:
                import salestracker.ui.gui  # noqa: F401
            except BaseException:
                with open(report, "w", encoding="utf-8") as fh:
                    traceback.print_exc(file=fh)
                raise SystemExit(1)
            raise SystemExit(0)
            """
        )
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "traceback.txt"
            completed = subprocess.run(
                [sys.executable, "-c", probe, str(report)],
                cwd=str(_PACKAGE_ROOT),
                capture_output=True,
                text=True,
            )
            detail = report.read_text(encoding="utf-8") if report.exists() else ""
        self.assertEqual(
            completed.returncode,
            0,
            "importing the GUI with no console streams failed: " + detail,
        )


class PaymentMethodDisplayTests(unittest.TestCase):
    """Methods read as words on screen but stay lowercase in the ledger."""

    def test_capitalizes_each_known_method(self) -> None:
        self.assertEqual(
            [format_payment_method(m) for m in PAYMENT_METHODS],
            ["Cash", "Venmo", "Other"],
        )

    def test_tolerates_blank_and_odd_input(self) -> None:
        self.assertEqual(format_payment_method(""), "")
        self.assertEqual(format_payment_method("  venmo  "), "Venmo")
        self.assertEqual(format_payment_method("VENMO"), "Venmo")

    def test_display_form_is_accepted_back_as_a_value(self) -> None:
        # The GUI combobox hands back what it shows, so the display form has
        # to survive a round trip through the parser.
        for method in PAYMENT_METHODS:
            with self.subTest(method=method):
                shown = format_payment_method(method)
                self.assertEqual(parse_payment_method(shown), method)


@unittest.skipUnless(HAVE_TK, "no display available for tkinter")
class GuiPresentationTests(unittest.TestCase):
    """Dialog placement, wheel routing, and payment-method labelling."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "sales.db")
        from gui import SalesApp

        self.app = SalesApp(self.db, auto_setup=False)
        self.addCleanup(self.app.destroy)
        self.addCleanup(self.app.tracker.close)
        self.app.tracker.add_product(name="Honey", unit="jar", unit_price="12.50")
        self.app.refresh()
        self.app.update_idletasks()

    def _log(self, purchaser: str, shown_method: str) -> None:
        self.app.var_purchaser.set(purchaser)
        self.app.var_qty.set("4")
        self.app.var_method.set(shown_method)
        self.app.log_order()
        self.app.update_idletasks()

    def test_method_picker_offers_capitalized_labels(self) -> None:
        dialog = self.app.open_log_dialog()
        self.addCleanup(dialog.destroy)
        self.assertEqual(
            list(self.app.method_combo["values"]), ["Cash", "Venmo", "Other"]
        )

    def test_picking_a_label_stores_the_lowercase_value(self) -> None:
        self._log("Ann", "Venmo")
        self.assertEqual(self.app.var_error.get(), "")
        stored = [o.payment_method for o in self.app.tracker.list_orders()]
        self.assertEqual(stored, ["venmo"])

    def test_list_shows_the_capitalized_label(self) -> None:
        self._log("Ann", "Venmo")
        rows = self.app.tree.get_children()
        self.assertEqual(self.app.tree.item(rows[0], "values")[8], "Venmo")

    def test_export_keeps_the_lowercase_value(self) -> None:
        # The CSV is data the CLI round-trips, so it must not be prettified.
        self._log("Ann", "Venmo")
        target = Path(self.tmp.name) / "out.csv"
        self.app.tracker.export_csv(target)
        body = target.read_text(encoding="utf-8")
        self.assertIn("venmo", body)
        self.assertNotIn("Venmo", body)

    def _dialog_is_over_parent(self, dialog) -> bool:
        dialog.update_idletasks()
        self.app.update_idletasks()
        dx = abs(
            (dialog.winfo_rootx() + dialog.winfo_width() // 2)
            - (self.app.winfo_rootx() + self.app.winfo_width() // 2)
        )
        dy = abs(
            (dialog.winfo_rooty() + dialog.winfo_height() // 2)
            - (self.app.winfo_rooty() + self.app.winfo_height() // 2)
        )
        # Allowance covers the window border and title bar, which winfo_root*
        # does not include.
        return dx <= 60 and dy <= 60

    def test_dialogs_open_centered_on_the_main_window(self) -> None:
        from gui import OrderEditor, ProductEditor, ProductWizard, SettingsDialog

        order = self.app.tracker.add_order(purchaser="Ann", quantity="1")
        product_id = order.product_id
        for factory in (
            lambda: ProductWizard(self.app, self.app.tracker, lambda: None),
            lambda: SettingsDialog(self.app, self.app.tracker, lambda: None),
            lambda: OrderEditor(self.app, self.app.tracker, order.id, lambda _o: None),
            lambda: ProductEditor(
                self.app, self.app.tracker, product_id, lambda _p, _n: None
            ),
        ):
            dialog = factory()
            dialog.update_idletasks()
            with self.subTest(dialog=type(dialog).__name__):
                centered = self._dialog_is_over_parent(dialog)
                geometry = dialog.winfo_geometry()
                dialog.destroy()
                self.assertTrue(
                    centered,
                    "dialog did not open over the main window: " + geometry,
                )

    def test_scrolling_dialogs_can_reach_all_their_content(self) -> None:
        from gui import SettingsDialog

        for factory in (
            lambda: SettingsDialog(self.app, self.app.tracker, lambda: None),
        ):
            dialog = factory()
            dialog.update_idletasks()
            canvas = self._find_canvas(dialog)
            with self.subTest(dialog=type(dialog).__name__):
                self.assertIsNotNone(canvas, "dialog has no scrolling body")
                region = canvas.cget("scrollregion")
                wheel_bound = bool(dialog.bind("<MouseWheel>"))
                dialog.destroy()
                self.assertTrue(region, "scrollregion was never set")
                self.assertTrue(
                    wheel_bound,
                    "the wheel is not bound on the toplevel, so children "
                    "added later would not scroll",
                )

    def _wheel_positions(self, target, deltas):
        """Scroll from the top with each delta and report where it ended up.

        The event goes to the widget the pointer would be over, because that
        is what decides which bindtags -- and so which handler -- see it.
        """
        seen = {}
        for delta in deltas:
            target.yview_moveto(0)
            target.update()
            # A real gesture is many events. Enough of them here that the
            # smallest delta still crosses a whole unit on the Tk builds that
            # round to one, without assuming the smoother fractional path.
            for _ in range(8):
                target.event_generate("<MouseWheel>", delta=delta, x=50, y=50)
                target.update()
            seen[delta] = target.yview()[0]
        return seen

    def test_touchpad_sized_wheel_deltas_scroll_dialogs(self) -> None:
        # A classic mouse notch is 120; a precision touchpad sends much less
        # per event. Dividing by 120 and truncating to an int discarded all of
        # them, so the dialog only moved when dragged by its scrollbar.
        from gui import SettingsDialog

        dialog = SettingsDialog(self.app, self.app.tracker, lambda: None)
        dialog.update()
        canvas = self._find_canvas(dialog)
        self.assertIsNotNone(canvas)
        self.assertLess(
            canvas.yview()[1], 1.0, "dialog does not overflow, nothing to test"
        )
        seen = self._wheel_positions(canvas, (-120, -40, -20, -12))
        dialog.destroy()
        for delta, position in seen.items():
            with self.subTest(delta=delta):
                self.assertGreater(
                    position, 0.0, f"a gesture of 8 events at delta {delta} moved nothing"
                )

    def test_touchpad_sized_wheel_deltas_scroll_the_order_list(self) -> None:
        # This one rides Tk's own Treeview binding rather than ours; the test
        # is here so a future hand-rolled binding cannot quietly regress it.
        for i in range(60):
            self.app.tracker.add_order(purchaser=f"Buyer {i:02d}", quantity="2")
        self.app.refresh()
        self.app.show_page("details")
        self.app.update()
        tree = self.app.tree
        self.assertLess(tree.yview()[1], 1.0, "list does not overflow")
        seen = self._wheel_positions(tree, (-120, -40, -20, -12))
        for delta, position in seen.items():
            with self.subTest(delta=delta):
                self.assertGreater(
                    position, 0.0, f"a gesture of 8 events at delta {delta} moved nothing"
                )

    def _visible_buttons(self):
        """Labels of every button currently on screen, in the main window."""
        from tkinter import ttk

        labels = []
        stack = list(self.app.winfo_children())
        while stack:
            widget = stack.pop()
            if isinstance(widget, ttk.Button) and widget.winfo_ismapped():
                labels.append(str(widget.cget("text")))
            stack.extend(widget.winfo_children())
        return labels

    def test_a_product_can_still_be_added_once_one_exists(self) -> None:
        # The empty-state button is swapped out as soon as a product exists,
        # which left the wizard reachable only by the menu or Ctrl+N.
        self.app.refresh()
        # A full update, not just update_idletasks: nothing reports itself
        # mapped until the window has actually been laid out.
        self.app.update()
        self.assertTrue(
            self.app.tracker.list_products(), "fixture should have a product"
        )
        labels = self._visible_buttons()
        self.assertIn(
            "New product",
            labels,
            "no visible control opens the product wizard; on screen: "
            + repr(labels),
        )

    def test_the_new_product_button_opens_the_wizard(self) -> None:
        from tkinter import ttk

        from gui import ProductWizard

        button = None
        stack = list(self.app.winfo_children())
        while stack:
            widget = stack.pop()
            if isinstance(widget, ttk.Button) and str(widget.cget("text")) == "New product":
                button = widget
                break
            stack.extend(widget.winfo_children())
        self.assertIsNotNone(button)
        button.invoke()
        self.app.update_idletasks()
        wizards = [
            child
            for child in self.app.winfo_children()
            if isinstance(child, ProductWizard)
        ]
        for wizard in wizards:
            wizard.destroy()
        self.assertEqual(len(wizards), 1, "the button did not open the wizard")

    def test_wheel_over_a_picker_moves_the_picker_not_the_panel(self) -> None:
        # One flick should move one thing. The panel's handler used to be
        # bound onto the picker as well, so a flick over the list moved the
        # list and the panel behind it at the same time.
        from tkinter import ttk

        from gui import SettingsDialog

        dialog = SettingsDialog(self.app, self.app.tracker, lambda: None)
        dialog.update()
        canvas = self._find_canvas(dialog)
        picker = None
        stack = list(dialog.winfo_children())
        while stack:
            widget = stack.pop()
            if isinstance(widget, ttk.Treeview):
                picker = widget
                break
            stack.extend(widget.winfo_children())
        self.assertIsNotNone(picker, "settings has no picker list")

        canvas.yview_moveto(0)
        dialog.update()
        before = canvas.yview()[0]
        for _ in range(8):
            picker.event_generate("<MouseWheel>", delta=-40, x=10, y=10)
            picker.update()
        after = canvas.yview()[0]
        dialog.destroy()
        self.assertEqual(
            after, before, "the panel scrolled while the pointer was on a list"
        )

    @staticmethod
    def _find_canvas(widget):
        import tkinter as tk

        stack = list(widget.winfo_children())
        while stack:
            child = stack.pop()
            if isinstance(child, tk.Canvas):
                return child
            stack.extend(child.winfo_children())
        return None


@unittest.skipUnless(HAVE_TK, "no display available for tkinter")
class GuiEditTests(unittest.TestCase):
    """The edit dialogs reached from the main window."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "sales.db")
        from gui import SalesApp

        self.app = SalesApp(self.db, auto_setup=False)
        self.addCleanup(self.app.destroy)
        self.addCleanup(self.app.tracker.close)
        self.tracker = self.app.tracker
        self.honey = self.tracker.add_product(name="Honey", unit="jar", unit_price="12.50")
        self.jam = self.tracker.add_product(name="Jam", unit="jar", unit_price="4.00")
        self.order = self.tracker.add_order(purchaser="Jim", quantity="10", product="Honey")
        self.tracker.set_received(self.order.id, "5")
        self.app.refresh()
        self.app.update_idletasks()

    def _select(self) -> None:
        self.app.tree.selection_set(str(self.order.id))
        self.app._on_select()

    def test_edit_order_needs_a_selection(self) -> None:
        self.app.tree.selection_remove(*self.app.tree.selection())
        self.assertIsNone(self.app.open_order_editor())
        self.assertIn("Select an order", self.app.var_error.get())

    def test_edit_order_updates_the_row(self) -> None:
        self._select()
        dialog = self.app.open_order_editor()
        self.assertEqual(dialog.var_purchaser.get(), "Jim")
        self.assertEqual(dialog.var_method.get(), "Cash")
        dialog.var_purchaser.set("Jimmy")
        dialog.var_product.set("Jam")
        dialog.var_qty.set("12")
        dialog.var_method.set("Venmo")
        dialog.save()
        self.app.update_idletasks()
        self.assertFalse(dialog.winfo_exists())

        edited = self.tracker.get_order(self.order.id)
        self.assertEqual(edited.purchaser, "Jimmy")
        self.assertEqual(edited.product_name, "Jam")
        self.assertEqual(edited.quantity_ordered, Decimal("12"))
        self.assertEqual(edited.quantity_received, Decimal("5"))
        self.assertEqual(edited.payment_method, "venmo")
        values = self.app.tree.item(str(self.order.id), "values")
        self.assertEqual(values[1], "Jimmy")
        self.assertEqual(values[3], "12 jar")
        self.assertEqual(values[4], "5")
        self.assertEqual(values[8], "Venmo")
        self.assertEqual(self.app.tree.selection(), (str(self.order.id),))

    def test_rejected_order_edit_stays_open_and_writes_nothing(self) -> None:
        self._select()
        dialog = self.app.open_order_editor()
        self.addCleanup(dialog.destroy)
        dialog.var_purchaser.set("Ann")
        dialog.var_qty.set("3")
        dialog.save()
        self.assertTrue(dialog.winfo_exists())
        self.assertIn("already received", dialog.var_error.get())
        self.assertEqual(self.tracker.get_order(self.order.id).purchaser, "Jim")

    def test_product_editor_starts_on_the_selected_orders_product(self) -> None:
        self.app.var_product.set("Jam")
        self._select()
        dialog = self.app.open_product_editor()
        self.addCleanup(dialog.destroy)
        self.assertEqual(dialog.var_name.get(), "Honey")
        self.assertIn("1 order(s)", dialog.var_attached.get())
        # Switching the picker loads the other product's fields.
        dialog.var_pick.set("Jam")
        dialog._load()
        self.assertEqual(dialog.var_price.get(), "4.00")
        self.assertEqual(dialog.var_attached.get(), "")

    def test_price_change_asks_before_repricing_orders(self) -> None:
        self._select()
        dialog = self.app.open_product_editor()
        dialog.var_price.set("14")
        with patch("gui.messagebox.askyesno", return_value=False) as asked:
            dialog.save()
        asked.assert_called_once()
        self.assertTrue(dialog.winfo_exists())
        self.assertEqual(self.tracker.get_product(self.honey.id).unit_price,
                         Decimal("12.50"))
        with patch("gui.messagebox.askyesno", return_value=True):
            dialog.save()
        self.assertFalse(dialog.winfo_exists())
        self.assertEqual(self.tracker.get_product(self.honey.id).unit_price,
                         Decimal("14.00"))

    def test_edit_without_a_price_change_does_not_ask(self) -> None:
        self._select()
        dialog = self.app.open_product_editor()
        dialog.var_notes.set("raw, unfiltered")
        with patch("gui.messagebox.askyesno") as asked:
            dialog.save()
        asked.assert_not_called()
        self.assertEqual(self.tracker.get_product(self.honey.id).notes, "raw, unfiltered")

    def test_renaming_the_form_product_keeps_the_form_on_it(self) -> None:
        self.app.var_product.set("Jam")
        self.app.tree.selection_remove(*self.app.tree.selection())
        dialog = self.app.open_product_editor()
        self.assertEqual(dialog.var_name.get(), "Jam")
        dialog.var_name.set("Strawberry jam")
        dialog.save()
        self.app.update_idletasks()
        self.assertEqual(self.app.var_product.get(), "Strawberry jam")
        log = self.app.open_log_dialog()
        self.addCleanup(log.destroy)
        self.assertIn("Strawberry jam", self.app.product_combo["values"])

    def test_duplicate_name_is_shown_in_the_dialog(self) -> None:
        self.app.tree.selection_remove(*self.app.tree.selection())
        self.app.var_product.set("Jam")
        dialog = self.app.open_product_editor()
        self.addCleanup(dialog.destroy)
        dialog.var_name.set("honey")
        dialog.save()
        self.assertTrue(dialog.winfo_exists())
        self.assertIn("already on file", dialog.var_error.get())


@unittest.skipUnless(HAVE_TK, "no display available for tkinter")
class GuiLayoutTests(unittest.TestCase):
    """Pages, the Counter cards, the Details grid, and the rest of the window."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "sales.db")
        from gui import SalesApp

        self.app = SalesApp(self.db, auto_setup=False)
        self.addCleanup(self.app.destroy)
        self.addCleanup(self.app.tracker.close)
        tracker = self.tracker = self.app.tracker
        tracker.add_product(name="Honey", unit="jar", unit_price="12.50")
        tracker.add_product(name="Candle", unit="each", unit_price="8.00")
        self.jim = tracker.add_order(purchaser="Jim", quantity="3", product="Honey")
        self.ann = tracker.add_order(purchaser="ann", quantity="2", product="Candle",
                                     payment_method="venmo")
        self.bob = tracker.add_order(purchaser="Bob", quantity="1", product="Honey")
        tracker.mark_received(self.bob.id)
        self.app.refresh()
        self.app.update_idletasks()

    def _select(self, order) -> None:
        self.app.tree.selection_set(str(order.id))
        self.app._on_select()

    def test_every_page_can_be_shown_and_only_one_at_a_time(self) -> None:
        for key, _title in self.app.PAGES:
            self.app.show_page(key)
            self.app.update()
            shown = [k for k, page in self.app.pages.items() if page.winfo_ismapped()]
            self.assertEqual(shown, [key])

    def test_counter_shows_waiting_cards_and_folds_the_collected(self) -> None:
        self.assertEqual(sorted(self.app._card_widgets), [self.jim.id, self.ann.id])
        self.app._toggle_collected()
        self.assertEqual(
            sorted(self.app._card_widgets), [self.jim.id, self.ann.id, self.bob.id]
        )

    def test_opening_a_card_unfolds_one_stepper_at_a_time(self) -> None:
        self.app.open_card(self.jim.id)
        self.app.update()
        self.assertIn("entry", self.app._card_widgets[self.jim.id])
        self.assertIn("open", self.app._card_widgets[self.ann.id])
        self.assertEqual(self.app.var_got.get(), "0")
        self.assertEqual(self.app._selected_id(), self.jim.id)
        self.app.open_card(self.ann.id)
        self.assertIn("open", self.app._card_widgets[self.jim.id])
        self.assertIn("entry", self.app._card_widgets[self.ann.id])
        self.app.close_card()
        self.assertIn("open", self.app._card_widgets[self.ann.id])

    def test_search_narrows_the_counter(self) -> None:
        self.app.var_search.set("ji")
        self.assertEqual(list(self.app._card_widgets), [self.jim.id])
        self.app.var_search.set("zzz")
        self.assertEqual(self.app._card_widgets, {})
        self.app.var_search.set("")
        self.assertEqual(len(self.app._card_widgets), 2)

    def test_log_dialog_logs_through_the_apps_variables(self) -> None:
        dialog = self.app.open_log_dialog()
        self.assertIs(self.app.open_log_dialog(), dialog)
        self.app.var_purchaser.set("Dana")
        self.app.var_product.set("Candle")
        self.app.var_qty.set("0")
        dialog.save()
        self.assertIn("greater than zero", self.app.var_error.get())
        self.assertTrue(dialog.winfo_exists())
        self.app.var_qty.set("4")
        dialog.save()
        self.app.update_idletasks()
        self.assertFalse(dialog.winfo_exists())
        self.assertIsNone(self.app.log_dialog)
        logged = [o for o in self.tracker.list_orders() if o.purchaser == "Dana"]
        self.assertEqual(len(logged), 1)
        self.assertEqual(logged[0].product_name, "Candle")
        self.assertEqual(self.app._selected_id(), logged[0].id)

    def test_command_line_previews_then_logs(self) -> None:
        self.app.var_cmd.set("Dana, 4")
        self.assertIn("Choose which product", self.app.var_cmd_preview.get())
        self.app.var_cmd.set("Dana, 4, Candle, venmo")
        self.assertTrue(self.app.var_cmd_preview.get().startswith("\u2192 Dana"))
        self.assertIn("$32.00", self.app.var_cmd_preview.get())
        self.app.run_cmd()
        logged = [o for o in self.tracker.list_orders() if o.purchaser == "Dana"]
        self.assertEqual(logged[0].payment_method, "venmo")
        self.assertEqual(self.app.var_cmd.get(), "")
        self.assertIn("Logged #", self.app.var_status.get())
        self.app.var_cmd.set("Nobody, 0, Candle")
        self.app.run_cmd()
        self.assertIn("greater than zero", self.app.var_status.get())
        self.assertEqual(len(self.tracker.list_orders()), 4)

    def test_grid_cell_editor_commits_and_reports(self) -> None:
        self.app.show_page("details")
        self.app.update()
        self._select(self.jim)
        self.assertEqual(self.app.begin_edit(), "break")
        self.assertIsNotNone(self.app._editor)
        self.app.var_got.set("2")
        self.app.update_received()
        self.assertEqual(self.tracker.get_order(self.jim.id).quantity_received, 2)
        self.assertIsNone(self.app._editor)
        self.app.begin_edit()
        self.app.var_got.set("9")
        self.app.update_received()
        self.assertIn("cannot be more than 3", self.app.var_status.get())
        self.assertEqual(self.tracker.get_order(self.jim.id).quantity_received, 2)

    def test_steppers_hand_over_one_at_a_time_within_bounds(self) -> None:
        self._select(self.jim)
        self.app.step_received(-1)
        self.assertEqual(self.tracker.get_order(self.jim.id).quantity_received, 0)
        for expected in (1, 2, 3, 3):
            self.app.step_received(1)
            self.assertEqual(
                self.tracker.get_order(self.jim.id).quantity_received, Decimal(expected)
            )
        self.assertEqual(self.app._selected_id(), self.jim.id)
        self.app.step_received(-1)
        self.assertEqual(self.tracker.get_order(self.jim.id).quantity_received, 2)

    def test_hand_over_all(self) -> None:
        self._select(self.jim)
        self.app.mark_all_received()
        self.assertTrue(self.tracker.get_order(self.jim.id).fulfilled)
        self.app._toggle_collected()
        self.app.open_card(self.jim.id)
        self.assertTrue(self.app._card_widgets[self.jim.id]["all"].instate(["disabled"]))
        self.assertTrue(self.app._card_widgets[self.jim.id]["plus"].instate(["disabled"]))

    def test_rejected_received_figure_is_reported_on_the_card(self) -> None:
        self.app.open_card(self.jim.id)
        self.app.var_got.set("9")
        self.app.update_received()
        self.assertIn("cannot be more than 3", self.app._card_error)
        self.assertIn("entry", self.app._card_widgets[self.jim.id])
        self.assertEqual(self.tracker.get_order(self.jim.id).quantity_received, 0)
        self.app.var_got.set("1")
        self.app.update_received()
        self.assertEqual(self.app._card_error, "")
        self.assertEqual(self.tracker.get_order(self.jim.id).quantity_received, 1)

    def test_paid_by_picker_changes_the_stored_method(self) -> None:
        self._select(self.jim)
        self.app.row_method.set("Venmo")
        self.app._change_method()
        self.assertEqual(self.tracker.get_order(self.jim.id).payment_method, "venmo")
        self.assertEqual(self.app._selected_id(), self.jim.id)

    def test_clicking_a_heading_sorts_and_a_second_click_reverses(self) -> None:
        def names():
            return [self.app.tree.item(i, "values")[1] for i in self.app.tree.get_children()]

        self.assertEqual(names(), ["Jim", "ann", "Bob"])
        self.app.sort_by("purchaser")
        self.assertEqual(names(), ["ann", "Bob", "Jim"])
        self.assertTrue(self.app.tree.heading("purchaser", "text").endswith("\u25b2"))
        self.app.sort_by("purchaser")
        self.assertEqual(names(), ["Jim", "Bob", "ann"])
        self.app.sort_by("owed")
        self.assertEqual(names()[-1], "Jim")

    def test_filter_pills_show_counts(self) -> None:
        texts = {k: str(p.cget("text")) for k, p in self.app._pills.items()}
        self.assertTrue(texts["all"].endswith("3"))
        self.assertTrue(texts["outstanding"].endswith("2"))
        self.assertTrue(texts["received"].endswith("1"))

    def test_placeholders_never_reach_the_variables(self) -> None:
        self.assertEqual(self.app.var_search.get(), "")
        self.app.update()
        # Shown while empty, gone once there is text.
        self.assertIn("Find someone\u2026", _mapped_label_texts(self.app))
        self.app.var_search.set("Zed")
        self.app.update()
        self.assertNotIn("Find someone\u2026", _mapped_label_texts(self.app))
        self.assertEqual(self.app.var_search.get(), "Zed")

    def test_buyers_page_groups_by_name_regardless_of_case(self) -> None:
        self.tracker.add_order(purchaser="Ann", quantity="1", product="Honey")
        self.app.refresh()
        self.app.show_page("buyers")
        tree = self.app.buyers_tree
        groups = tree.get_children()
        self.assertEqual(len(groups), 3)
        ann = [g for g in groups if g == "buyer:ann"][0]
        self.assertEqual(len(tree.get_children(ann)), 2)
        self.app.var_buyer_search.set("bo")
        self.assertEqual(tree.get_children(), ("buyer:bob",))

    def test_opening_an_order_from_buyers_selects_it_on_details(self) -> None:
        self.app.var_filter.set("received")
        self.app.refresh()
        self.app.show_page("buyers")
        self.app.buyers_tree.selection_set(str(self.jim.id))
        self.app._open_from_buyers()
        self.assertEqual(self.app._page, "details")
        self.assertEqual(self.app._selected_id(), self.jim.id)

    def test_products_page_has_a_card_per_product(self) -> None:
        self.app.show_page("products")
        self.app.update()
        titles = [str(w.cget("text")) for w in _labels(self.app.products_body)
                  if str(w.cget("style")) == "CardTitle.TLabel"]
        self.assertEqual(titles, ["Candle", "Honey"])

    def test_money_page_checks_the_drawer_against_cash_only(self) -> None:
        self._select(self.jim)
        self.app.mark_all_received()  # $37.50 cash collected, plus Bob's $12.50
        self.app.show_page("money")
        panel = self.app.money_panel
        self.assertEqual(panel.var_expected.get(), "$50.00")
        panel.var_counts[50].set("1")
        self.assertIn("Balanced", panel.var_verdict.get())
        self.assertEqual(panel.var_difference.get(), "$0.00")
        panel.var_counts[1].set("2")
        self.assertIn("Over", panel.var_verdict.get())
        self.assertEqual(panel.var_difference.get(), "+$2.00")

    def test_sidebar_badges_count_what_needs_attention(self) -> None:
        badge = self.app._nav["counter"]["badge"]
        self.assertEqual(str(badge.cget("text")), "2")
        self.assertEqual(str(self.app._nav["products"]["badge"].cget("text")), "2")

    def test_each_unit_reads_as_times(self) -> None:
        self.app.var_product.set("Candle")
        self.assertEqual(self.app.var_qty_label.get(), "\u00d7")
        self.app.var_product.set("Honey")
        self.assertEqual(self.app.var_qty_label.get(), "jar of")

    def test_welcome_replaces_the_form_until_a_product_exists(self) -> None:
        from gui import SalesApp

        empty = SalesApp(str(Path(self.tmp.name) / "empty.db"), auto_setup=False)
        self.addCleanup(empty.destroy)
        self.addCleanup(empty.tracker.close)
        empty.update()
        self.assertTrue(empty.need_product.winfo_ismapped())
        self.assertFalse(empty.order_form.winfo_ismapped())
        empty.tracker.add_product(name="Honey", unit="jar", unit_price="1")
        empty.refresh()
        empty.update()
        self.assertFalse(empty.need_product.winfo_ismapped())
        self.assertTrue(empty.order_form.winfo_ismapped())


def _labels(widget):
    from tkinter import ttk as _ttk

    found, stack = [], [widget]
    while stack:
        node = stack.pop(0)
        for child in node.winfo_children():
            if isinstance(child, _ttk.Label):
                found.append(child)
            stack.append(child)
    return found


def _mapped_label_texts(widget):
    return [str(w.cget("text")) for w in _labels(widget) if w.winfo_ismapped()]



class UpdateProtocolTests(unittest.TestCase):
    """The update manifest, sources, checking, verifying and installing."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings: dict[str, str] = {}

    def _updater(self, fetch=None, version="0.1.5", now=None, platform="linux-x86_64"):
        from salestracker.update import Updater

        return Updater(
            version,
            lambda key, default="": self.settings.get(key, default),
            lambda key, value: self.settings.__setitem__(key, value) or value,
            fetch=fetch or (lambda *a, **k: self.fail("unexpected fetch")),
            now=now or datetime.now,
            platform=platform,
        )

    def _publish(self, folder: Path, version: str = "0.2.0") -> Path:
        """A release folder: two fake binaries and their manifest."""
        from salestracker.update import write_manifest

        folder.mkdir(parents=True, exist_ok=True)
        (folder / "SalesTracker.exe").write_bytes(b"MZ windows build " + version.encode())
        (folder / "SalesTracker-linux-x86_64").write_bytes(b"\x7fELF linux build " + version.encode())
        text = write_manifest(
            [folder / "SalesTracker.exe", folder / "SalesTracker-linux-x86_64"],
            version, "", notes="Fixes and things.",
        )
        (folder / "update.json").write_text(text, encoding="utf-8")
        return folder

    def test_versions_compare_numerically(self) -> None:
        from salestracker.update import UpdateError, is_newer, parse_version

        self.assertEqual(parse_version("v0.2.0"), (0, 2, 0))
        self.assertTrue(is_newer("0.10.0", "0.9.9"))
        self.assertFalse(is_newer("0.1.5", "0.1.5"))
        self.assertFalse(is_newer("v0.1.4", "0.1.5"))
        with self.assertRaises(UpdateError):
            parse_version("latest")

    def test_release_file_names_map_to_platforms(self) -> None:
        from salestracker.update import asset_key_for_name

        self.assertEqual(asset_key_for_name("SalesTracker.exe"), "windows-x86_64")
        self.assertEqual(asset_key_for_name("SalesTracker-linux-x86_64"), "linux-x86_64")
        self.assertIsNone(asset_key_for_name("update.json"))

    def test_manifest_round_trips_with_checksums(self) -> None:
        from salestracker.update import parse_manifest, sha256_of

        folder = self._publish(self.root / "rel", "0.3.0")
        release = parse_manifest((folder / "update.json").read_text())
        self.assertEqual(release.version, "0.3.0")
        self.assertEqual(release.notes, "Fixes and things.")
        linux = release.asset_for("linux-x86_64")
        self.assertEqual(linux.name, "SalesTracker-linux-x86_64")
        self.assertEqual(linux.size, (folder / linux.name).stat().st_size)
        self.assertEqual(linux.sha256, sha256_of(folder / linux.name))
        with self.assertRaisesRegex(TrackerError, "no build for this platform"):
            release.asset_for("macos-arm64")

    def test_manifest_rejects_junk(self) -> None:
        from salestracker.update import UpdateError, parse_manifest

        for text in ("not json", "[]", '{"notes": "x"}', '{"version": "1.0", "assets": {"linux-x86_64": {"name": "a"}}}'):
            with self.subTest(text=text), self.assertRaises(UpdateError):
                parse_manifest(text)

    def test_changelog_notes_take_one_section(self) -> None:
        from salestracker.update import changelog_notes

        text = "# Changelog\n\n## [Unreleased]\n- later\n\n## [0.2.0] - 2026-10-01\n\n### Added\n- Updates.\n\n## [0.1.5] - 2026-09-16\n- old\n"
        self.assertEqual(changelog_notes(text, "0.2.0"), "### Added\n- Updates.")
        self.assertEqual(changelog_notes(text, "0.0.1"), "")

    def test_sources_resolve_to_a_manifest_and_a_base(self) -> None:
        from salestracker.update import UpdateError, resolve_asset_url, resolve_source

        github = resolve_source(None)
        self.assertEqual(github.kind, "github")
        self.assertEqual(github.owner, "j0nsh1n")
        self.assertTrue(github.manifest.endswith("/releases/latest/download/update.json"))
        url = resolve_source("https://example.org/releases")
        self.assertEqual(url.manifest, "https://example.org/releases/update.json")
        self.assertEqual(resolve_asset_url(url, "SalesTracker.exe"),
                         "https://example.org/releases/SalesTracker.exe")
        self.assertEqual(resolve_asset_url(url, "https://cdn.example.org/x"),
                         "https://cdn.example.org/x")
        direct = resolve_source("https://example.org/feed/latest.json")
        self.assertEqual(direct.manifest, "https://example.org/feed/latest.json")
        self.assertEqual(direct.base, "https://example.org/feed/")
        folder = resolve_source(str(self.root))
        self.assertEqual(folder.kind, "folder")
        self.assertEqual(Path(folder.manifest), self.root / "update.json")
        self.assertEqual(resolve_asset_url(folder, "SalesTracker.exe"),
                         str(self.root / "SalesTracker.exe"))
        with self.assertRaises(UpdateError):
            resolve_source("github:nope")

    def test_check_from_a_folder_reports_a_newer_version(self) -> None:
        folder = self._publish(self.root / "rel", "0.2.0")
        updater = self._updater()
        updater.set_source(str(folder))
        release = updater.check()
        self.assertEqual(release.version, "0.2.0")
        self.assertTrue(updater.available(release))
        self.assertIn("update_last_check", self.settings)
        self.assertFalse(updater.due())

    def test_check_is_due_once_a_day(self) -> None:
        clock = {"now": datetime(2026, 10, 1, 9, 0)}
        folder = self._publish(self.root / "rel")
        updater = self._updater(now=lambda: clock["now"])
        updater.set_source(str(folder))
        self.assertTrue(updater.due())
        updater.check()
        clock["now"] = datetime(2026, 10, 1, 20, 0)
        self.assertFalse(updater.due())
        clock["now"] = datetime(2026, 10, 2, 9, 30)
        self.assertTrue(updater.due())

    def test_web_source_uses_the_etag_cache(self) -> None:
        from salestracker.update import Response

        folder = self._publish(self.root / "rel")
        body = (folder / "update.json").read_bytes()
        calls = []

        def fetch(url, headers=None, timeout=20.0):
            calls.append((url, dict(headers or {})))
            if headers and headers.get("If-None-Match") == '"abc"':
                return Response(304, {}, b"")
            return Response(200, {"etag": '"abc"'}, body)

        updater = self._updater(fetch=fetch)
        updater.set_source("https://example.org/dl/")
        first = updater.check()
        second = updater.check()
        self.assertEqual((first.version, second.version), ("0.2.0", "0.2.0"))
        self.assertEqual(calls[0][0], "https://example.org/dl/update.json")
        self.assertNotIn("If-None-Match", calls[0][1])
        self.assertEqual(calls[1][1]["If-None-Match"], '"abc"')

    def test_rate_limit_is_explained_not_crashed(self) -> None:
        from salestracker.update import Response, UpdateError

        reset = int(datetime(2026, 10, 1, 14, 30).timestamp())
        updater = self._updater(fetch=lambda *a, **k: Response(
            403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)}, b""))
        with self.assertRaisesRegex(UpdateError, "rate-limiting this address until 14:30"):
            updater.check()

    def test_private_repo_needs_a_token_and_then_uses_the_api(self) -> None:
        from salestracker.update import Response, UpdateError

        folder = self._publish(self.root / "rel")
        manifest = (folder / "update.json").read_bytes()
        listing = json.dumps({"assets": [
            {"name": "update.json", "url": "https://api.github.com/repos/o/r/releases/assets/1"},
            {"name": "SalesTracker-linux-x86_64", "url": "https://api.github.com/repos/o/r/releases/assets/2"},
        ]}).encode()
        calls = []

        def fetch(url, headers=None, timeout=20.0):
            calls.append((url, dict(headers or {})))
            if url.endswith("/releases/latest/download/update.json"):
                return Response(404, {}, b"")
            if url.endswith("/releases/latest"):
                return Response(200, {}, listing)
            if url.endswith("/assets/1"):
                self.assertEqual(headers["Accept"], "application/octet-stream")
                return Response(200, {}, manifest)
            if url.endswith("/assets/2"):
                return Response(200, {}, (folder / "SalesTracker-linux-x86_64").read_bytes())
            self.fail("unexpected url " + url)

        updater = self._updater(fetch=fetch)
        updater.set_source("github:o/r")
        with self.assertRaisesRegex(UpdateError, "private, add a token"):
            updater.check()
        updater.set_token("ghp_secret")
        release = updater.check()
        self.assertEqual(release.version, "0.2.0")
        self.assertTrue(all(h.get("Authorization") == "Bearer ghp_secret"
                            for u, h in calls if "api.github.com" in u))
        # The binary is fetched through its API url with the token.
        fresh = updater.download(release, self.root / "into")
        self.assertEqual(fresh.name, "SalesTracker-linux-x86_64.new")
        self.assertEqual(calls[-1][0], "https://api.github.com/repos/o/r/releases/assets/2")
        self.assertEqual(calls[-1][1]["Authorization"], "Bearer ghp_secret")

    def test_token_is_dropped_on_a_redirect_to_another_host(self) -> None:
        import urllib.request
        from salestracker.update import _NoTokenAcrossHosts

        handler = _NoTokenAcrossHosts()
        request = urllib.request.Request(
            "https://api.github.com/x", headers={"Authorization": "Bearer t", "Accept": "a"}
        )
        moved = handler.redirect_request(request, None, 302, "Found", {},
                                         "https://objects.githubusercontent.com/y")
        self.assertFalse(moved.has_header("Authorization"))
        self.assertTrue(moved.has_header("Accept"))
        same = handler.redirect_request(request, None, 302, "Found", {},
                                        "https://api.github.com/z")
        self.assertTrue(same.has_header("Authorization"))

    def test_download_verifies_size_and_checksum(self) -> None:
        from salestracker.update import UpdateError

        folder = self._publish(self.root / "rel")
        updater = self._updater()
        updater.set_source(str(folder))
        release = updater.check()
        fresh = updater.download(release, self.root / "into")
        self.assertTrue(fresh.exists())
        # Tamper with the published file: the copy is refused and removed.
        (folder / "SalesTracker-linux-x86_64").write_bytes(b"\x7fELF something else")
        with self.assertRaisesRegex(UpdateError, "arrived incomplete"):
            updater.download(release, self.root / "into2")
        self.assertFalse((self.root / "into2" / "SalesTracker-linux-x86_64.new").exists())
        (folder / "SalesTracker-linux-x86_64").write_bytes(b"\x7fELF linux build 0.2.X")
        with self.assertRaisesRegex(UpdateError, "did not match its published checksum"):
            updater.download(release, self.root / "into3")

    def test_install_keeps_the_previous_build_and_can_restore_it(self) -> None:
        from salestracker.update import UpdateError, Updater

        target = self.root / "SalesTracker"
        target.write_bytes(b"old build")
        fresh = self.root / "SalesTracker.new"
        fresh.write_bytes(b"new build")
        self.assertIsNone(Updater.previous(target))
        installed = Updater.install(fresh, target)
        self.assertEqual(installed, target)
        self.assertEqual(target.read_bytes(), b"new build")
        self.assertEqual(Updater.previous(target).read_bytes(), b"old build")
        self.assertFalse(fresh.exists())
        if os.name != "nt":
            self.assertTrue(os.access(target, os.X_OK))
        Updater.restore_previous(target)
        self.assertEqual(target.read_bytes(), b"old build")
        self.assertEqual(Updater.previous(target).read_bytes(), b"new build")
        # From a source checkout there is nothing to install into.
        with self.assertRaisesRegex(UpdateError, "packaged build only"):
            Updater.install(fresh, None)

    def test_http_source_end_to_end(self) -> None:
        import http.server
        import threading
        from salestracker.update import http_fetch

        folder = self._publish(self.root / "www", "0.4.0")
        handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(folder))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.RequestHandlerClass.log_message = lambda *a, **k: None
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        port = server.server_address[1]

        updater = self._updater(fetch=http_fetch)
        updater.set_source(f"http://127.0.0.1:{port}/")
        release = updater.check()
        self.assertEqual(release.version, "0.4.0")
        fresh = updater.download(release, self.root / "got")
        self.assertEqual(fresh.read_bytes(), (folder / "SalesTracker-linux-x86_64").read_bytes())
        updater.set_source(f"http://127.0.0.1:{port}/missing/")
        with self.assertRaisesRegex(TrackerError, "answered 404"):
            updater.check()

    def test_cli_update_reports_and_refuses_to_install_from_source(self) -> None:
        folder = self._publish(self.root / "rel", "9.9.9")
        db = str(self.root / "sales.db")
        out = io.StringIO()
        with patch("sys.stdout", out):
            self.assertEqual(main(["--db", db, "update", "--source", str(folder)]), 0)
        self.assertIn("Version 9.9.9 is available", out.getvalue())
        # The source is remembered.
        with SalesTracker(db) as tracker:
            self.assertEqual(tracker.get_setting("update_source"), str(folder))
        err = io.StringIO()
        with patch("sys.stdout", io.StringIO()), patch("sys.stderr", err):
            self.assertEqual(main(["--db", db, "update", "--install"]), 1)
        self.assertIn("pass --yes", err.getvalue())
        err = io.StringIO()
        with patch("sys.stdout", io.StringIO()), patch("sys.stderr", err):
            self.assertEqual(main(["--db", db, "update", "--install", "--yes"]), 1)
        self.assertIn("packaged build only", err.getvalue())

    def test_manifest_tool_prints_json(self) -> None:
        from salestracker.update import main as update_main

        folder = self._publish(self.root / "rel")
        out = io.StringIO()
        with patch("sys.stdout", out):
            code = update_main([
                "write-manifest", "--version", "v0.5.0", "--base-url", "https://h/d/",
                str(folder / "SalesTracker.exe"), str(folder / "SalesTracker-linux-x86_64"),
            ])
        self.assertEqual(code, 0)
        data = json.loads(out.getvalue())
        self.assertEqual(data["version"], "0.5.0")
        self.assertEqual(data["assets"]["windows-x86_64"]["url"], "https://h/d/SalesTracker.exe")


@unittest.skipUnless(HAVE_TK, "no display available for tkinter")
class GuiUpdateTests(unittest.TestCase):
    """The Updates section of Settings and the quiet startup check."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        from gui import SalesApp

        self.app = SalesApp(str(self.root / "sales.db"), auto_setup=False)
        self.addCleanup(self.app.destroy)
        self.addCleanup(self.app.tracker.close)
        self.folder = self.root / "rel"
        self.folder.mkdir()
        from salestracker.update import platform_key, write_manifest

        name = "SalesTracker.exe" if platform_key().startswith("windows") else "SalesTracker-linux-x86_64"
        (self.folder / name).write_bytes(b"build 9.0.0")
        (self.folder / "update.json").write_text(
            write_manifest([self.folder / name], "9.0.0", "", notes="Big news."), encoding="utf-8"
        )

    def _dialog(self):
        from gui import SettingsDialog

        dialog = SettingsDialog(self.app, self.app.tracker, on_change=self.app.refresh)
        self.addCleanup(dialog.destroy)
        return dialog

    def test_check_now_reports_the_newer_version(self) -> None:
        dialog = self._dialog()
        self.assertIn("Not checked yet", dialog.var_update_status.get())
        self.assertTrue(dialog.install_button.instate(["disabled"]))
        dialog.var_update_source.set(str(self.folder))
        dialog._check_updates(sync=True)
        self.assertIn("Version 9.0.0 is available", dialog.var_update_status.get())
        self.assertIn("Big news.", dialog.var_update_status.get())
        self.assertTrue(dialog.install_button.instate(["!disabled"]))
        self.assertEqual(self.app.tracker.get_setting("update_source"), str(self.folder))

    def test_a_bad_source_is_reported_in_place(self) -> None:
        dialog = self._dialog()
        dialog.var_update_source.set(str(self.root / "nowhere"))
        dialog._check_updates(sync=True)
        self.assertIn("No update.json", dialog.var_update_status.get())
        self.assertEqual(str(dialog.update_status.cget("style")), "Error.TLabel")

    def test_quiet_check_puts_a_notice_in_the_status_bar(self) -> None:
        self.app.updates.updater.set_source(str(self.folder))
        self.app.check_updates_quietly(sync=True)
        self.assertIn("Version 9.0.0 is available", self.app.var_notice.get())

    def test_install_from_settings_swaps_the_binary_and_relaunches(self) -> None:
        dialog = self._dialog()
        dialog.var_update_source.set(str(self.folder))
        dialog._check_updates(sync=True)
        target = self.root / "SalesTracker"
        target.write_bytes(b"build 0.1.5")
        launched = []
        with patch("salestracker.update.target_path", return_value=target), \
             patch("gui.messagebox.askyesno", return_value=True), \
             patch("gui.messagebox.showinfo"), \
             patch.object(self.app.updates, "relaunch", lambda t: launched.append(t)), \
             patch.object(self.app, "_on_close"):
            dialog._install_update(sync=True)
        self.assertEqual(target.read_bytes(), b"build 9.0.0")
        self.assertEqual((self.root / "SalesTracker.old").read_bytes(), b"build 0.1.5")
        self.assertEqual(launched, [target])


class ThemeTests(unittest.TestCase):
    """Palette bookkeeping and how a stored choice becomes a palette."""

    def test_both_palettes_define_the_same_tokens(self) -> None:
        # A token missing from one palette is a crash the moment someone
        # switches theme, not a visual glitch.
        self.assertEqual(
            set(theme.PALETTES[theme.LIGHT]),
            set(theme.PALETTES[theme.DARK]),
        )

    def test_every_token_is_a_hex_colour(self) -> None:
        for name, palette in theme.PALETTES.items():
            for token, value in palette.items():
                with self.subTest(theme=name, token=token):
                    self.assertRegex(value, r"^#[0-9A-Fa-f]{6}$")

    def test_light_and_dark_actually_differ(self) -> None:
        self.assertNotEqual(
            theme.PALETTES[theme.LIGHT]["BG"], theme.PALETTES[theme.DARK]["BG"]
        )

    def test_unknown_choices_fall_back_to_system(self) -> None:
        for value in ("", None, "puce", "  "):
            with self.subTest(value=value):
                self.assertEqual(theme.normalize_choice(value), theme.SYSTEM)

    def test_choices_are_case_and_space_insensitive(self) -> None:
        self.assertEqual(theme.normalize_choice("  Dark "), theme.DARK)

    def test_explicit_choices_ignore_the_desktop(self) -> None:
        with patch.object(theme, "detect_os_theme", return_value=theme.DARK):
            self.assertEqual(theme.resolve(theme.LIGHT), theme.LIGHT)
        with patch.object(theme, "detect_os_theme", return_value=theme.LIGHT):
            self.assertEqual(theme.resolve(theme.DARK), theme.DARK)

    def test_system_follows_the_desktop(self) -> None:
        for reported in (theme.LIGHT, theme.DARK):
            with self.subTest(reported=reported):
                with patch.object(theme, "detect_os_theme", return_value=reported):
                    self.assertEqual(theme.resolve(theme.SYSTEM), reported)

    def test_detection_never_raises(self) -> None:
        # It shells out on some platforms; a missing tool must not take the
        # app down on startup.
        self.assertIn(theme.detect_os_theme(), (theme.LIGHT, theme.DARK))


@unittest.skipUnless(HAVE_TK, "no display available for tkinter")
class GuiThemeTests(unittest.TestCase):
    """The appearance choice is remembered and repaints the running window."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "sales.db")

    def _app(self):
        from gui import SalesApp

        app = SalesApp(self.db, auto_setup=False)
        # A test may shut the window itself to reopen it; tearing down twice
        # is not an error worth failing on.
        self.addCleanup(self._quietly, app.destroy)
        self.addCleanup(self._quietly, app.tracker.close)
        app.update_idletasks()
        return app

    @staticmethod
    def _quietly(action) -> None:
        try:
            action()
        except Exception:
            pass

    def test_defaults_to_system_on_a_fresh_ledger(self) -> None:
        app = self._app()
        self.assertEqual(app.theme_choice, theme.SYSTEM)
        self.assertIn(app.theme_painted, (theme.LIGHT, theme.DARK))

    def test_choice_is_remembered_across_restarts(self) -> None:
        app = self._app()
        app.set_theme(theme.DARK)
        self.assertEqual(app.tracker.get_setting("theme"), theme.DARK)
        app.tracker.close()
        app.destroy()

        reopened = self._app()
        self.assertEqual(reopened.theme_choice, theme.DARK)
        self.assertEqual(reopened.theme_painted, theme.DARK)

    def test_switching_repaints_the_window_and_the_module_palette(self) -> None:
        # The palette lives on the implementation module, not the shim.
        from salestracker.ui import gui as gui_module

        app = self._app()
        app.set_theme(theme.DARK)
        app.update_idletasks()
        self.assertEqual(gui_module.BG, theme.PALETTES[theme.DARK]["BG"])
        self.assertEqual(app.cget("bg"), theme.PALETTES[theme.DARK]["BG"])

        app.set_theme(theme.LIGHT)
        app.update_idletasks()
        self.assertEqual(gui_module.BG, theme.PALETTES[theme.LIGHT]["BG"])
        self.assertEqual(app.cget("bg"), theme.PALETTES[theme.LIGHT]["BG"])

    def test_open_dialogs_repaint_too(self) -> None:
        from gui import SettingsDialog

        app = self._app()
        app.set_theme(theme.LIGHT)
        dialog = SettingsDialog(app, app.tracker, lambda: None)
        dialog.update_idletasks()
        self.assertEqual(dialog.cget("bg"), theme.PALETTES[theme.LIGHT]["PANEL"])

        app.set_theme(theme.DARK)
        app.update_idletasks()
        dialog.update_idletasks()
        panel = theme.PALETTES[theme.DARK]["PANEL"]
        canvases = [c.cget("bg") for c in gui_canvases(dialog)]
        dialog_bg = dialog.cget("bg")
        dialog.destroy()
        self.assertEqual(dialog_bg, panel)
        self.assertEqual(canvases, [panel])

    def test_settings_offers_every_choice(self) -> None:
        from gui import SettingsDialog

        app = self._app()
        dialog = SettingsDialog(app, app.tracker, lambda: None)
        dialog.update_idletasks()
        labels = collect_radio_labels(dialog)
        dialog.destroy()
        self.assertEqual(labels, ["System", "Light", "Dark"])

    def test_choosing_in_settings_applies_it(self) -> None:
        from gui import SettingsDialog

        app = self._app()
        dialog = SettingsDialog(app, app.tracker, lambda: None)
        dialog.var_theme.set(theme.DARK)
        dialog._change_theme()
        app.update_idletasks()
        dialog.destroy()
        self.assertEqual(app.theme_choice, theme.DARK)
        self.assertEqual(app.tracker.get_setting("theme"), theme.DARK)

    def test_money_page_rules_repaint(self) -> None:
        app = self._app()
        app.set_theme(theme.LIGHT)
        app.show_page("money")
        app.update_idletasks()
        rules = app.money_panel._rules
        self.assertTrue(rules, "the money page lost its separators")

        app.set_theme(theme.DARK)
        app.update_idletasks()
        line = theme.PALETTES[theme.DARK]["LINE"]
        self.assertEqual([rule.cget("bg") for rule in rules], [line] * len(rules))

    def test_page_surfaces_repaint(self) -> None:
        # Plain Tk canvases on the page and on cards must not be left in the
        # dialog colour the generic repaint gives every canvas.
        app = self._app()
        app.set_theme(theme.LIGHT)
        app.tracker.add_product(name="Honey", unit="jar", unit_price="1")
        app.tracker.add_order(purchaser="Jim", quantity="2")
        app.refresh()
        app.show_page("products")
        app.set_theme(theme.DARK)
        app.update_idletasks()
        dark = theme.PALETTES[theme.DARK]
        self.assertTrue(app._bars)
        self.assertTrue(all(bar.cget("bg") == dark["CARD"] for bar in app._bars))
        self.assertEqual(app.brand_mark.cget("bg"), dark["SIDEBAR"])
        # The page's own scroll canvas sits on PAGE; the product cards' bars
        # sit on CARD and are covered by the assertion above.
        canvases = [c for c in gui_canvases(app.pages["products"]) if c not in app._bars]
        self.assertTrue(canvases)
        self.assertTrue(all(c.cget("bg") == dark["PAGE"] for c in canvases))

    def test_combobox_dropdown_rebuilds_in_the_new_palette(self) -> None:
        app = self._app()
        app.set_theme(theme.LIGHT)
        app.tracker.add_product(name="Honey", unit="jar", unit_price="1")
        app.refresh()
        dialog = app.open_log_dialog()
        self.addCleanup(self._quietly, dialog.destroy)
        combo = app.method_combo
        # The dropdown listbox is only built on the first open; force that
        # first open here instead of simulating a click.
        combo.tk.call("ttk::combobox::PopdownWindow", combo)
        app.update_idletasks()
        self.assertEqual(
            app.tk.call(combobox_listbox(app, combo), "cget", "-background"),
            theme.PALETTES[theme.LIGHT]["FIELD"],
        )

        app.set_theme(theme.DARK)
        app.update_idletasks()
        # The stale popdown is gone, and the next open rebuilds it in the
        # new palette.
        self.assertFalse(combo.tk.call("winfo", "exists", f"{combo}.popdown"))
        combo.tk.call("ttk::combobox::PopdownWindow", combo)
        self.assertEqual(
            app.tk.call(combobox_listbox(app, combo), "cget", "-background"),
            theme.PALETTES[theme.DARK]["FIELD"],
        )

    def test_os_theme_poll_reschedules_at_the_platform_interval(self) -> None:
        app = self._app()
        # A non-system choice skips the detection, so only the reschedule
        # is under test here.
        app.theme_choice = theme.LIGHT
        scheduled: dict[str, int] = {}
        app.after = lambda ms, func=None: scheduled.setdefault("ms", ms)
        app._watch_os_theme()
        self.assertEqual(scheduled["ms"], theme.OS_THEME_POLL_MS)

    def test_poll_interval_is_gentler_where_detection_spawns(self) -> None:
        # Windows answers a registry read; the other platforms spawn a
        # subprocess per check and must not do that every few seconds.
        if sys.platform == "win32":
            self.assertEqual(theme.OS_THEME_POLL_MS, 4000)
        else:
            self.assertGreater(theme.OS_THEME_POLL_MS, 4000)


def gui_canvases(widget):
    from salestracker.ui import gui as gui_module

    return gui_module._descendant_canvases(widget)


def combobox_listbox(app, combo):
    """Path to the dropdown's plain Tk listbox, wherever this Tk buries it.

    Tk 9 keeps it at `<combo>.popdown.l`, 8.6 at `<combo>.popdown.f.l`, and
    the popdown is created by Tcl, so this hunts by widget class rather than
    through Python's widget registry.
    """

    def hunt(path):
        for child in app.tk.splitlist(app.tk.call("winfo", "children", path)):
            if app.tk.call("winfo", "class", child) == "Listbox":
                return str(child)
            found = hunt(child)
            if found:
                return found
        return None

    found = hunt(f"{combo}.popdown")
    if found is None:
        raise AssertionError("the combobox popdown has no listbox")
    return found


def collect_radio_labels(widget):
    from tkinter import ttk as _ttk

    found = []
    stack = [widget]
    while stack:
        node = stack.pop(0)
        for child in node.winfo_children():
            if isinstance(child, _ttk.Radiobutton):
                text = str(child.cget("text"))
                if text in ("System", "Light", "Dark"):
                    found.append(text)
            stack.append(child)
    return found


if __name__ == "__main__":
    unittest.main()
