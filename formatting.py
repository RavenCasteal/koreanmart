"""
Stage 8 - shared number / unit formatting for every screen.

Pure display helpers only (no SQL, no Tkinter). Before Stage 8 each page
built its own f-string, which produced strings like "5000 piece" or
"1 grams". Every quantity + unit pair now goes through format_quantity(),
so the Dashboard, Ingredients, Recipe, Calculator, Inventory, History and
Monitoring screens all read the same way.

Units stored in SQLite are kept exactly as typed by the user; this module
only chooses a singular/plural form for the common count nouns. Unknown
units (ml, kg, l, a custom unit...) are shown verbatim.
"""

from __future__ import annotations

import math
from typing import Any

# lower-case unit -> (singular form, plural form)
_UNIT_FORMS: dict[str, tuple[str, str]] = {
    "gram": ("gram", "grams"),
    "grams": ("gram", "grams"),
    "piece": ("piece", "pieces"),
    "pieces": ("piece", "pieces"),
    "sheet": ("sheet", "sheets"),
    "sheets": ("sheet", "sheets"),
    "egg": ("egg", "eggs"),
    "eggs": ("egg", "eggs"),
    "clove": ("clove", "cloves"),
    "cloves": ("clove", "cloves"),
    "slice": ("slice", "slices"),
    "slices": ("slice", "slices"),
    "bunch": ("bunch", "bunches"),
    "bunches": ("bunch", "bunches"),
}

_EPSILON = 1e-9


def _as_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def format_number(value: Any) -> str:
    """Whole numbers stay plain (not 1e+06); fractions keep short decimals."""
    number = _as_float(value)
    if number is None:
        return str(value)
    if number == int(number):
        return str(int(number))
    return f"{number:g}"


def unit_label(value: Any, unit: Any) -> str:
    """Return the singular/plural unit for this quantity, or the unit as-is."""
    text = str(unit or "").strip()
    if not text:
        return ""
    forms = _UNIT_FORMS.get(text.lower())
    if forms is None:
        return text
    number = _as_float(value)
    if number is not None and abs(abs(number) - 1) < _EPSILON:
        return forms[0]
    return forms[1]


def format_quantity(value: Any, unit: Any = "", *, signed: bool = False) -> str:
    """e.g. 1 gram, 5000 pieces, 400 ml, +250 grams, -2 pieces, 2.5 grams."""
    number = _as_float(value)
    text = format_number(value)
    if signed and number is not None and number > 0:
        text = "+" + text
    label = unit_label(value, unit)
    return f"{text} {label}" if label else text


# ----------------------------------------------------------------------
# Stage 9 - money. The restaurant is based in the Philippines, so ALL prices,
# totals, tendered amounts and change use Philippine peso (PHP). Never use
# yen, CNY, USD or a bare number for a price shown to a customer.
# ----------------------------------------------------------------------
CURRENCY_CODE = "PHP"
CURRENCY_SYMBOL = "\u20b1"  # ₱


def format_money(value: Any) -> str:
    """Peso amount with thousands separator and two decimals, e.g. ₱1,250.50."""
    number = _as_float(value)
    if number is None:
        number = 0.0
    return f"{CURRENCY_SYMBOL}{number:,.2f}"


# Readable alias used by the POS screen and the receipt builder.
format_peso = format_money
