"""
Stage 16 - automatic change ("auto deduct") for the cashier POS.

The cashier only has to enter / tap the cash the customer handed over. This
module subtracts the (possibly discounted) total from it and reports the
change, so nobody has to do mental arithmetic at the counter. It also suggests
which bills / coins to hand back.

Pure logic: no Tkinter, no SQL, no third-party dependency. It never writes
anything. The money rules MATCH store_sales.confirm_sale():

  * amount parsing is identical to products.validate_money();
  * amounts are rounded to two decimals (centavos);
  * total_amount is the NET payable amount after any Senior/PWD discount.

store_sales.confirm_sale() remains the authority: it recomputes the total and
the change INSIDE the BEGIN IMMEDIATE transaction. This module is only a live
preview, so a stale UI can never corrupt a committed sale.
"""

from __future__ import annotations

import math
from typing import Any

from formatting import CURRENCY_SYMBOL, format_money

EPSILON = 1e-9

# One-tap buttons for the bills a customer normally hands over in the
# Philippines. Anything else (coins, two bills, exact e-wallet amount) can
# still be typed into the Amount tendered field.
QUICK_CASH: tuple[int, ...] = (20, 50, 100, 200, 500, 1000)

# Bill / coin values used to SUGGEST a change breakdown, in centavos. This is
# only a hint for the cashier - there is no cash drawer integration.
DENOMINATIONS: tuple[tuple[int, str], ...] = (
    (100000, "1,000"),
    (50000, "500"),
    (20000, "200"),
    (10000, "100"),
    (5000, "50"),
    (2000, "20"),
    (1000, "10"),
    (500, "5"),
    (100, "1"),
    (25, "0.25"),
    (10, "0.10"),
    (5, "0.05"),
    (1, "0.01"),
)

STATUS_NO_BASKET = "no_basket"
STATUS_EMPTY = "empty"
STATUS_INVALID = "invalid"
STATUS_INSUFFICIENT = "insufficient"
STATUS_EXACT = "exact"
STATUS_DUE = "due"

# Public so the UI can colour the label without hardcoding status strings.
GOOD_STATUSES: tuple[str, ...] = (STATUS_EXACT, STATUS_DUE)
BAD_STATUSES: tuple[str, ...] = (STATUS_INVALID, STATUS_INSUFFICIENT)


def round_money(value: Any) -> float:
    """Centavo rounding, identical to store_sales._money()."""
    return round(float(value) + EPSILON, 2)


def parse_tendered(value: Any) -> float:
    """Parse cash received with the SAME rules as products.validate_money()."""
    text = str(value if value is not None else "").strip()
    try:
        number = float(text)
    except (TypeError, ValueError):
        raise ValueError("Amount tendered must be a number.") from None
    if not math.isfinite(number):
        raise ValueError("Amount tendered must be a number.")
    if number < 0:
        raise ValueError("Amount tendered cannot be negative.")
    return round(number, 2)


def breakdown(amount: Any) -> list[tuple[str, int]]:
    """Greedy bill/coin breakdown, e.g. 82 -> 50, 20, 10, 1, 1."""
    cents = int(round(round_money(amount) * 100))
    parts: list[tuple[str, int]] = []
    for value, label in DENOMINATIONS:
        count, cents = divmod(cents, value)
        if count:
            parts.append((label, count))
    return parts


def format_breakdown(amount: Any) -> str:
    """'1 x ₱50 + 1 x ₱20 + 1 x ₱10 + 2 x ₱1' (symbol comes from formatting)."""
    parts = [
        f"{count} x {CURRENCY_SYMBOL}{label}"
        for label, count in breakdown(amount)
    ]
    return " + ".join(parts)


def quick_cash_label(amount: Any) -> str:
    """Compact button caption, e.g. '₱1,000' (no trailing .00)."""
    number = int(round(float(amount)))
    return f"{CURRENCY_SYMBOL}{number:,}"


def compute(total: Any, tendered: Any) -> dict[str, Any]:
    """Auto-deduct: subtract the net total from the cash received.

    Never raises. Returns a display dict with:
        status, label, detail, total, tendered, change, short, ok
    `ok` means the cash received covers the total; the repository still does
    the real check at confirm time.
    """
    base = round_money(total)
    text = str(tendered if tendered is not None else "").strip()
    result: dict[str, Any] = {
        "status": STATUS_NO_BASKET,
        "label": "Scan items to see the change.",
        "detail": "",
        "total": base,
        "tendered": 0.0,
        "change": 0.0,
        "short": 0.0,
        "ok": False,
    }
    if base <= 0:
        return result
    if not text:
        result["status"] = STATUS_EMPTY
        result["label"] = "Tap a bill or type the cash received."
        result["detail"] = "Change is worked out automatically."
        return result
    try:
        amount = parse_tendered(text)
    except ValueError as exc:
        result["status"] = STATUS_INVALID
        result["label"] = str(exc)
        return result
    result["tendered"] = amount
    if amount + EPSILON < base:
        result["status"] = STATUS_INSUFFICIENT
        result["short"] = round_money(base - amount)
        result["label"] = f"Short by {format_money(result['short'])}"
        result["detail"] = "Ask the customer for more cash."
        return result

    result["ok"] = True
    result["change"] = round_money(amount - base)
    if result["change"] <= EPSILON:
        result["status"] = STATUS_EXACT
        result["label"] = "Exact amount - no change."
    else:
        result["status"] = STATUS_DUE
        result["label"] = f"Change due: {format_money(result['change'])}"
        result["detail"] = "Give back: " + format_breakdown(result["change"])
    return result
