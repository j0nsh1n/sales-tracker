#!/usr/bin/env python3
"""Command-line interface and interactive session."""

from __future__ import annotations

import argparse
import sys
from typing import Callable, TextIO

from salestracker.models import (
    DEFAULT_DB,
    UNITS,
    Order,
    Product,
    Summary,
    TrackerError,
    format_money,
    format_qty,
)
from salestracker.store import SalesTracker

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
