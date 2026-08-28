#!/usr/bin/env python3
"""Tests for the Sales Tracker core library, CLI, and GUI."""

from __future__ import annotations

import ast
import csv
import io
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from decimal import Decimal
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
        self.assertEqual(values[0], "Jim")
        self.assertIn("5 / 10", values[2])
        self.assertEqual(values[4], "outstanding")

        self.app.var_got.set("10")
        self.app.update_received()
        self.app.update_idletasks()
        self.assertEqual(len(self.app.tree.get_children()), 1)
        values = self.app.tree.item(rows[0], "values")
        self.assertEqual(values[4], "received")

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
        self.assertEqual(self.app.tree.item(rows[0], "values")[-1], "Venmo")

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
        from gui import MoneyDialog, ProductWizard, SettingsDialog

        for factory in (
            lambda: ProductWizard(self.app, self.app.tracker, lambda: None),
            lambda: SettingsDialog(self.app, self.app.tracker, lambda: None),
            lambda: MoneyDialog(self.app, self.app.tracker),
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
        from gui import MoneyDialog, SettingsDialog

        for factory in (
            lambda: SettingsDialog(self.app, self.app.tracker, lambda: None),
            lambda: MoneyDialog(self.app, self.app.tracker),
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
        self.app.update()
        tree = self.app.tree
        self.assertLess(tree.yview()[1], 1.0, "list does not overflow")
        seen = self._wheel_positions(tree, (-120, -40, -20, -12))
        for delta, position in seen.items():
            with self.subTest(delta=delta):
                self.assertGreater(
                    position, 0.0, f"a gesture of 8 events at delta {delta} moved nothing"
                )

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

    def test_open_money_dialog_rules_repaint(self) -> None:
        from gui import MoneyDialog

        app = self._app()
        app.set_theme(theme.LIGHT)
        dialog = MoneyDialog(app, app.tracker)
        dialog.update_idletasks()
        self.assertTrue(dialog._rules, "the money page lost its separators")

        app.set_theme(theme.DARK)
        app.update_idletasks()
        line = theme.PALETTES[theme.DARK]["LINE"]
        bgs = [rule.cget("bg") for rule in dialog._rules]
        dialog.destroy()
        self.assertEqual(bgs, [line] * len(bgs))

    def test_combobox_dropdown_rebuilds_in_the_new_palette(self) -> None:
        app = self._app()
        app.set_theme(theme.LIGHT)
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
