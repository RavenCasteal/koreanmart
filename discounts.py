"""
Stage 15 - Senior Citizen / PWD discount rules (pure logic, no Tk or SQL).

Philippine retail practice grants a 20% discount to senior citizens and
persons with disabilities (PWD) on eligible purchases. This capstone applies
one discount to the WHOLE sale, requires the customer's name and ID number,
and never stacks two discounts. The result is an immutable money snapshot:

    subtotal_amount   sum of the scanned line totals (before discount)
    discount_rate     0.20 for Senior Citizen / PWD, otherwise 0
    discount_amount   subtotal * rate, rounded to 2 decimals
    total_amount      subtotal - discount (what the customer actually pays)

Keeping the arithmetic here (rather than in the GUI) means the POS preview,
the committed sale, the receipt and the Z-read all agree. No third-party
dependency, no database access, no eval().
"""

from __future__ import annotations

from typing import Any

NO_DISCOUNT = ""
SENIOR = "senior"
PWD = "pwd"

DISCOUNT_RATE = 0.20
MAX_NAME_LENGTH = 80
MAX_ID_LENGTH = 40

LABELS: dict[str, str] = {
    NO_DISCOUNT: "No discount",
    SENIOR: "Senior Citizen",
    PWD: "PWD",
}
CHOICES: tuple[str, ...] = (LABELS[NO_DISCOUNT], LABELS[SENIOR], LABELS[PWD])
SHORT_LABELS: dict[str, str] = {SENIOR: "Senior 20%", PWD: "PWD 20%"}

EPSILON = 1e-9

_ALIASES: dict[str, str] = {
    "": NO_DISCOUNT,
    "none": NO_DISCOUNT,
    "no discount": NO_DISCOUNT,
    "no": NO_DISCOUNT,
    "0": NO_DISCOUNT,
    "senior": SENIOR,
    "senior citizen": SENIOR,
    "senior citizen discount": SENIOR,
    "sc": SENIOR,
    "pwd": PWD,
    "pwd discount": PWD,
    "person with disability": PWD,
    "persons with disability": PWD,
    "disability": PWD,
}


def money(value: Any) -> float:
    """Round to centavos, matching store_sales._money()."""
    return round(float(value) + EPSILON, 2)


def normalize_discount_type(value: Any) -> str:
    """Accept a UI label or an internal code; return '' | 'senior' | 'pwd'."""
    text = str(value if value is not None else "").strip().lower()
    if text in _ALIASES:
        return _ALIASES[text]
    raise ValueError("Choose No discount, Senior Citizen or PWD.")


def label(discount_type: Any) -> str:
    """Human-readable label, or '' when there is no discount."""
    return LABELS.get(normalize_discount_type(discount_type), "")


def short_label(discount_type: Any) -> str:
    """Receipt-width label for the discount line."""
    return SHORT_LABELS.get(normalize_discount_type(discount_type), "")


def validate_customer(
    name: Any, id_number: Any, discount_type: Any
) -> tuple[str, str]:
    """Require proof of eligibility ONLY when a discount is being applied.

    A sale with no discount silently ignores stray text in the name/ID fields,
    so clearing the basket never leaves stale customer details on a sale.
    """
    kind = normalize_discount_type(discount_type)
    if kind == NO_DISCOUNT:
        return "", ""
    clean_name = " ".join(str(name if name is not None else "").split())
    clean_id = " ".join(str(id_number if id_number is not None else "").split())
    if len(clean_name) < 2:
        raise ValueError(
            "Enter the customer's name before applying a Senior/PWD discount."
        )
    if len(clean_name) > MAX_NAME_LENGTH:
        raise ValueError("The customer name is too long (80 characters max).")
    if len(clean_id) < 3:
        raise ValueError(
            "Enter the Senior Citizen / PWD ID number before applying a discount."
        )
    if len(clean_id) > MAX_ID_LENGTH:
        raise ValueError("The ID number is too long (40 characters max).")
    return clean_name, clean_id


def compute(subtotal: Any, discount_type: Any) -> dict[str, Any]:
    """Return the immutable money snapshot for one basket."""
    kind = normalize_discount_type(discount_type)
    base = money(subtotal)
    rate = DISCOUNT_RATE if kind != NO_DISCOUNT else 0.0
    amount = money(base * rate) if rate else 0.0
    total = money(base - amount)
    return {
        "subtotal_amount": base,
        "discount_type": kind,
        "discount_rate": rate,
        "discount_amount": amount,
        "total_amount": total,
        "discount_label": LABELS.get(kind, ""),
    }


def apply_to_sale(sale: dict[str, Any]) -> dict[str, Any]:
    """Fill in missing Stage 15 fields on a legacy sale dict (read-only copy)."""
    result = dict(sale)
    total = money(result.get("total_amount") or 0.0)
    base = money(result.get("subtotal_amount") or 0.0)
    if base <= 0:
        base = total
    result["subtotal_amount"] = base
    result["discount_type"] = str(result.get("discount_type") or NO_DISCOUNT)
    result["discount_rate"] = float(result.get("discount_rate") or 0.0)
    result["discount_amount"] = money(result.get("discount_amount") or 0.0)
    result["customer_name"] = str(result.get("customer_name") or "")
    result["customer_id"] = str(result.get("customer_id") or "")
    result["discount_label"] = LABELS.get(result["discount_type"], "")
    return result
