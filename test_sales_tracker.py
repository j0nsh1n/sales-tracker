#!/usr/bin/env python3
"""Tests for the Sales Tracker core library, CLI, and GUI."""

from __future__ import annotations

import io
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from sales_tracker import (
    InteractiveSession,
    SalesTracker,
    TrackerError,
    application_dir,
    collect_product_answers,
    format_money,
    main,
)


class SalesTrackerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "sales.db"
        self.tracker = SalesTracker(self.db)

    def tearDown(self) -> None:
        self.tracker.close()
        self.tmp.cleanup()

    def _product(self, name: str = "Honey", **kwargs):
        payload = dict(name=name, unit="jar", unit_price="12.50")
        payload.update(kwargs)
        return self.tracker.add_product(**payload)

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

    def test_no_single_order_delete(self) -> None:
        self.assertFalse(hasattr(self.tracker, "delete_order"))
        self.assertFalse(hasattr(self.tracker, "delete_sale"))

    def test_reset_orders_keeps_products(self) -> None:
        self._product()
        self.tracker.add_order(purchaser="Jim", quantity="10")
        self.tracker.add_order(purchaser="Ada", quantity="3")
        count = self.tracker.reset_orders()
        self.assertEqual(count, 2)
        self.assertEqual(self.tracker.list_orders(), [])
        self.assertEqual(len(self.tracker.list_products()), 1)

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

    def test_cli_rejects_order_without_product(self) -> None:
        code = main(["--db", self.db, "order", "--buyer", "Jim", "--qty", "2"])
        self.assertEqual(code, 1)


class GuiSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "sales.db")
        from gui import SalesApp

        self.app = SalesApp(self.db, auto_setup=False)
        self.app.update_idletasks()

    def tearDown(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
