#!/usr/bin/env python3
"""Domain types and parse/format helpers."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path


def application_dir() -> Path:
    """Directory that holds sales.db — next to the .exe when frozen."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


DEFAULT_DB = application_dir() / "sales.db"
MONEY_QUANT = Decimal("0.01")
# Only "cash" lands in the drawer; everything else settles elsewhere and must
# be excluded from a cash reconciliation.
PAYMENT_METHODS = ("cash", "venmo", "other")
CASH = "cash"
QTY_QUANT = Decimal("0.001")
UNITS = ("each", "jar", "box", "dozen", "lb", "bag", "case", "pack", "bottle")


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


def parse_payment_method(value: object) -> str:
    method = str(value or CASH).strip().lower()
    if not method:
        return CASH
    if method not in PAYMENT_METHODS:
        allowed = ", ".join(PAYMENT_METHODS)
        raise TrackerError(f"payment method must be one of: {allowed}.")
    return method


def format_money(amount: Decimal) -> str:
    return f"${amount:,.2f}"


def format_qty(quantity: Decimal) -> str:
    text = format(quantity, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def format_payment_method(method: str) -> str:
    """Payment method as it should be shown to a person.

    Display only. The stored value and the CSV column stay lowercase, because
    those are the contract the CLI takes on input and round-trips.
    """
    return str(method or "").strip().capitalize()


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
    payment_method: str = CASH

    @property
    def is_cash(self) -> bool:
        return self.payment_method == CASH

    @property
    def collected(self) -> Decimal:
        """Money already taken in: what has actually been handed over."""
        return (self.quantity_received * self.unit_price).quantize(
            MONEY_QUANT, rounding=ROUND_HALF_UP
        )

    @property
    def uncollected(self) -> Decimal:
        """Money still to come in for the part not yet handed over."""
        return (self.remaining * self.unit_price).quantize(
            MONEY_QUANT, rounding=ROUND_HALF_UP
        )

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
class Financials:
    """Expected money, split by whether it lands in the cash drawer."""

    cash_collected: Decimal
    cash_uncollected: Decimal
    other_collected: Decimal
    other_uncollected: Decimal

    @property
    def total_collected(self) -> Decimal:
        return self.cash_collected + self.other_collected

    @property
    def total_uncollected(self) -> Decimal:
        return self.cash_uncollected + self.other_uncollected

    @property
    def book_value(self) -> Decimal:
        return self.total_collected + self.total_uncollected


@dataclass(frozen=True)
class Summary:
    order_count: int
    outstanding_count: int
    received_count: int
    units_ordered: Decimal
    units_received: Decimal
    units_remaining: Decimal
    revenue: Decimal


