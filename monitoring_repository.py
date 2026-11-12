"""
Stage 7 - low-stock / expiration monitoring (READ-ONLY data layer).

One screen answers "what needs my attention right now?" by classifying every
ACTIVE ingredient on two independent axes:

  * stock   - Out of stock / Low stock / In stock   (repository.stock_status)
  * expiry  - Expired / Near expiry / OK / Not set  (repository.expiry_status)

Rules:
  * SELECT only. Like the Stage 6 calculator, this module opens a deferred
    read snapshot and rolls it back (never commits). Monitoring never edits
    a balance, never changes an expiry date and never records a transaction.
  * Soft-deleted (is_active = 0) ingredients are excluded: they are historical
    records, not things the kitchen needs to reorder.
  * "Near expiry" means within repository.EXPIRY_WARNING_DAYS (7 days),
    INCLUDING today. "Expired" means any date before today.
  * Priority = Critical (out of stock or expired), Warning (low stock or
    near expiry / invalid date), Healthy (everything else).

A `today` argument makes the classification deterministic for tests and for
future scheduled reports; the UI leaves it as None (real current date).

All SQL lives here; pages/monitoring.py only formats the returned dict.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, Iterator

import database
import repository

# Re-exported so the UI never has to reach into another module's namespace.
EXPIRY_WARNING_DAYS = repository.EXPIRY_WARNING_DAYS

# Filter codes accepted by get_monitoring_data().
FILTER_ALL = "ALL"
FILTER_ATTENTION = "ATTENTION"
FILTER_STOCK = "STOCK"
FILTER_EXPIRY = "EXPIRY"
FILTER_CRITICAL = "CRITICAL"

FILTER_CODES: dict[str, str] = {
    "All ingredients": FILTER_ALL,
    "Needs attention": FILTER_ATTENTION,
    "Stock alerts": FILTER_STOCK,
    "Expiry alerts": FILTER_EXPIRY,
    "Critical only": FILTER_CRITICAL,
}

SEVERITY_CRITICAL = "Critical"
SEVERITY_WARNING = "Warning"
SEVERITY_HEALTHY = "Healthy"

_SEVERITY_ORDER = {
    SEVERITY_CRITICAL: 0,
    SEVERITY_WARNING: 1,
    SEVERITY_HEALTHY: 2,
}


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _resolve_path(db_path: Any) -> Any:
    return database.DB_PATH if db_path is None else db_path


@contextmanager
def _read_snapshot(db_path: Any) -> Iterator[sqlite3.Connection]:
    """Consistent read snapshot; rollback (never commit) closes it."""
    conn = database.get_connection(db_path)
    try:
        conn.execute("BEGIN")
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    columns = [description[0] for description in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def resolve_today(today: Any = None) -> date:
    """Accept None, a date, or an ISO YYYY-MM-DD string."""
    if today is None:
        return date.today()
    if isinstance(today, datetime):
        return today.date()
    if isinstance(today, date):
        return today
    try:
        return date.fromisoformat(str(today).strip())
    except ValueError as exc:
        raise ValueError("Reference date must use the format YYYY-MM-DD.") from exc


def _days_left(expiration_date: str | None, today: date) -> int | None:
    if not expiration_date:
        return None
    try:
        return (date.fromisoformat(expiration_date) - today).days
    except ValueError:
        return None


def _expiry_text(
    label: str, expiration_date: str | None, days_left: int | None
) -> str:
    """Short, human-friendly expiry column value."""
    if label == "Not set":
        return "Not set"
    if label == "Invalid":
        return "Invalid date"
    if label == "Expired":
        return f"{expiration_date} · {abs(days_left or 0)} d ago"
    if label == "Near expiry":
        if days_left == 0:
            return f"{expiration_date} · today"
        return f"{expiration_date} · in {days_left} d"
    return str(expiration_date)


def _classify(raw: dict[str, Any], today: date) -> dict[str, Any]:
    """Turn one ingredients row into a monitoring row."""
    balance = float(raw["quantity_on_hand"])
    minimum = float(raw["minimum_stock"])
    expiry_value = raw["expiration_date"]

    stock = repository.stock_status(balance, minimum)
    expiry = repository.expiry_status(expiry_value, today)
    days_left = _days_left(expiry_value, today)

    reasons: list[str] = []
    if stock == "Out of stock":
        reasons.append("Out of stock")
    elif stock == "Low stock":
        reasons.append("Low stock")
    if expiry == "Expired":
        reasons.append("Expired")
    elif expiry == "Near expiry":
        reasons.append("Near expiry")
    elif expiry == "Invalid":
        reasons.append("Invalid expiry date")

    if "Out of stock" in reasons or "Expired" in reasons:
        severity = SEVERITY_CRITICAL
    elif reasons:
        severity = SEVERITY_WARNING
    else:
        severity = SEVERITY_HEALTHY

    return {
        "ingredient_id": int(raw["ingredient_id"]),
        "name": str(raw["name"]),
        "unit": str(raw["unit"]),
        "quantity_on_hand": balance,
        "minimum_stock": minimum,
        "expiration_date": expiry_value,
        "status": stock,
        "expiry_status": expiry,
        "days_left": days_left,
        "expiry_text": _expiry_text(expiry, expiry_value, days_left),
        "alert_reasons": reasons,
        "needs_attention": bool(reasons),
        "severity": severity,
    }


def _matches_filter(row: dict[str, Any], code: str) -> bool:
    if code == FILTER_ALL:
        return True
    if code == FILTER_ATTENTION:
        return row["needs_attention"]
    if code == FILTER_STOCK:
        return row["status"] != "In stock"
    if code == FILTER_EXPIRY:
        return row["expiry_status"] in ("Expired", "Near expiry", "Invalid")
    if code == FILTER_CRITICAL:
        return row["severity"] == SEVERITY_CRITICAL
    raise ValueError("Unknown monitoring filter.")


def _sort_key(row: dict[str, Any]) -> tuple[int, int, str]:
    days = row["days_left"] if row["days_left"] is not None else 10_000
    return (_SEVERITY_ORDER[row["severity"]], days, row["name"].lower())


# ----------------------------------------------------------------------
# Reads
# ----------------------------------------------------------------------
def get_active_ingredients(
    today: Any = None, db_path: Any = None
) -> list[dict[str, Any]]:
    """Every active ingredient with its stock / expiry classification."""
    reference = resolve_today(today)
    with _read_snapshot(_resolve_path(db_path)) as conn:
        raw_rows = _rows(
            conn.execute(
                """
                SELECT ingredient_id, name, unit, quantity_on_hand,
                       minimum_stock, expiration_date
                FROM ingredients
                WHERE is_active = 1
                ORDER BY name COLLATE NOCASE
                """
            )
        )
    return [_classify(row, reference) for row in raw_rows]


def get_monitoring_data(
    search: str = "",
    alert_filter: str = FILTER_ALL,
    today: Any = None,
    db_path: Any = None,
) -> dict[str, Any]:
    """Filtered monitoring table plus counts for the summary cards.

    `alert_filter` is one of FILTER_* codes (or a UI label from FILTER_CODES).
    Search matches name, unit, status, expiry date and expiry status.
    """
    reference = resolve_today(today)
    code = FILTER_CODES.get(str(alert_filter), str(alert_filter)).upper()
    if code not in (
        FILTER_ALL, FILTER_ATTENTION, FILTER_STOCK,
        FILTER_EXPIRY, FILTER_CRITICAL,
    ):
        raise ValueError("Unknown monitoring filter.")

    all_rows = get_active_ingredients(reference, db_path=db_path)

    keyword = str(search or "").strip().lower()
    filtered = [
        row for row in all_rows
        if _matches_filter(row, code)
        and (
            not keyword
            or keyword in row["name"].lower()
            or keyword in row["unit"].lower()
            or keyword in row["status"].lower()
            or keyword in row["expiry_status"].lower()
            or keyword in str(row["expiration_date"] or "").lower()
        )
    ]
    filtered.sort(key=_sort_key)

    def count(label: str) -> int:
        return sum(1 for row in all_rows if label in row["alert_reasons"])

    summary = {
        "warning_days": EXPIRY_WARNING_DAYS,
        "total_active": len(all_rows),
        "out_of_stock": count("Out of stock"),
        "low_stock": count("Low stock"),
        "near_expiry": count("Near expiry"),
        "expired": count("Expired"),
        "invalid_expiry": count("Invalid expiry date"),
        "needs_attention": sum(1 for row in all_rows if row["needs_attention"]),
        "healthy": sum(1 for row in all_rows if not row["needs_attention"]),
        "critical": sum(
            1 for row in all_rows if row["severity"] == SEVERITY_CRITICAL
        ),
    }
    summary["stock_alerts"] = summary["out_of_stock"] + summary["low_stock"]
    summary["expiry_alerts"] = summary["near_expiry"] + summary["expired"]

    return {
        "today": reference.isoformat(),
        "warning_days": EXPIRY_WARNING_DAYS,
        "filter": code,
        "search": str(search or "").strip(),
        "summary": summary,
        "rows": filtered,
        "all_rows": all_rows,
        "shown": len(filtered),
        "total": len(all_rows),
        "stock_alert_rows": [
            row for row in all_rows if row["status"] != "In stock"
        ],
        "expiry_alert_rows": [
            row for row in all_rows
            if row["expiry_status"] in ("Expired", "Near expiry", "Invalid")
        ],
    }


def get_alert_counts(
    today: Any = None, db_path: Any = None
) -> dict[str, int]:
    """Small helper for badges / future dashboard integration."""
    return get_monitoring_data(
        today=today, db_path=db_path
    )["summary"]
