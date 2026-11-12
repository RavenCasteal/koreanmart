"""
Stage 8 - ONE shared definition of the stock / expiry status rules.

Before Stage 8 the dashboard (database._ingredient_status) and the catalog
(repository.stock_status) each had their own copy of the same rule. They
agreed today, but two copies can silently drift apart later. This module has
no imports at all, so every layer can use it without a circular import:

    database.py            -> stock_status
    repository.py          -> stock_status, expiry_status (re-exported)
    monitoring_repository  -> uses them through repository

Nothing here touches SQLite. These are pure functions over plain values.
"""

from __future__ import annotations

from datetime import date

# "Near expiry" means within this many days, INCLUDING today.
EXPIRY_WARNING_DAYS = 7


def stock_status(quantity: float, minimum: float) -> str:
    """Classify a balance. Zero is out of stock, never also low stock."""
    if quantity <= 0:
        return "Out of stock"
    if quantity <= minimum:
        return "Low stock"
    return "In stock"


def expiry_status(expiration_date: str | None, today: date | None = None) -> str:
    """Classify an ISO YYYY-MM-DD expiry date (or a missing/invalid one)."""
    if not expiration_date:
        return "Not set"
    try:
        expiry = date.fromisoformat(expiration_date)
    except (TypeError, ValueError):
        return "Invalid"
    today = today or date.today()
    days_left = (expiry - today).days
    if days_left < 0:
        return "Expired"
    if days_left <= EXPIRY_WARNING_DAYS:
        return "Near expiry"
    return "OK"
