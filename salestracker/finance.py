"""Cash-drawer sanity check.

The ledger computes what should be in the drawer. The operator counts their
bills independently and enters the counts here. Nothing in this module reads
the ledger, which is the point: the two figures have to be arrived at
separately or the comparison proves nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from salestracker.models import MONEY_QUANT, TrackerError, format_money

# US paper currency, largest first so the count sheet reads like a till.
DENOMINATIONS = (100, 50, 20, 10, 5, 2, 1)


def parse_bill_count(value: object, *, denomination: int) -> int:
    """A count of bills: a whole number, never negative."""
    text = str(value if value is not None else "").strip()
    if not text:
        return 0
    try:
        count = Decimal(text)
    except (InvalidOperation, AttributeError) as exc:
        raise TrackerError(
            f"count of ${denomination} bills must be a whole number."
        ) from exc
    if not count.is_finite():
        raise TrackerError(
            f"count of ${denomination} bills must be a whole number."
        )
    if count < 0:
        raise TrackerError(f"count of ${denomination} bills cannot be negative.")
    if count != count.to_integral_value():
        raise TrackerError(
            f"count of ${denomination} bills must be a whole number "
            "(you cannot have half a bill)."
        )
    return int(count)


def count_cash(counts: dict[int, object]) -> Decimal:
    """Total value of a drawer count. Unlisted denominations count as zero."""
    total = Decimal("0.00")
    for denomination in DENOMINATIONS:
        number = parse_bill_count(counts.get(denomination), denomination=denomination)
        total += Decimal(denomination) * number
    return total.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Reconciliation:
    """The result of comparing a drawer count against expected cash."""

    expected: Decimal
    counted: Decimal

    @property
    def difference(self) -> Decimal:
        """Counted minus expected: positive is over, negative is short."""
        return (self.counted - self.expected).quantize(
            MONEY_QUANT, rounding=ROUND_HALF_UP
        )

    @property
    def balanced(self) -> bool:
        return self.difference == 0

    @property
    def state(self) -> str:
        if self.balanced:
            return "balanced"
        return "over" if self.difference > 0 else "short"

    @property
    def headline(self) -> str:
        if self.balanced:
            return "Balanced — the drawer matches the ledger."
        amount = format_money(abs(self.difference))
        if self.difference > 0:
            return f"Over by {amount} — more cash than the ledger expects."
        return f"Short by {amount} — less cash than the ledger expects."


def reconcile(expected: Decimal, counts: dict[int, object]) -> Reconciliation:
    return Reconciliation(
        expected=expected.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP),
        counted=count_cash(counts),
    )
