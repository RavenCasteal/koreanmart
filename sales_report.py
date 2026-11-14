"""
Stage 13 - read-only sales reporting and end-of-day closing (Z-read).

Stage 12 can record, void and reprint a sale, but a manager had no way to
answer "how did the store do today?". This module turns the IMMUTABLE sale
snapshots into a daily report:

    daily_summary()  - money, basket counts, payment / cashier / category /
                       product breakdowns and the day's transaction list
    sales_trend()    - a contiguous last-N-days series (zero days included)
    build_z_read()   - a fixed-width, printable end-of-day summary
    issue_z_read()   - archive to reports/ + best-effort Windows printing
    export_transactions_csv() - one UTF-8 CSV for the selected day

Design rules:
  * READ ONLY. No INSERT / UPDATE / DELETE is ever issued here. The only
    call that can touch the schema is products.ensure_schema(), the same
    idempotent "make sure Stage 12 tables exist" guard used by store_sales'
    read helpers; on a delivered database it is a no-op.
  * Every figure comes from store_sales / store_sale_items SNAPSHOTS, so a
    later rename, reprice or deactivation cannot rewrite an old day's report.
  * Voids are NEVER deleted. gross_sales includes every sale recorded that
    day at its PRE-discount subtotal; voided_amount is the pre-discount value
    of the voided ones, discount_amount is the 20% Senior/PWD relief granted
    on completed sales, and net_sales is what the store actually kept
    (gross - voids - discounts).
  * Stage 15: the discount breakdown, the discounted transaction rows, the
    Z-read and the CSV export all read the immutable Stage 15 sale columns.
    Legacy sales (subtotal_amount = 0) fall back to total_amount, so older
    days still report exactly as they did before Stage 15.
  * Manager only. The page is not in a cashier's sidebar AND daily_summary /
    sales_trend call permissions.require(), so a cashier session cannot read
    sales totals even if a widget is forced open.
  * All money is Philippine peso (PHP / peso sign), formatted only through
    formatting.format_money(). No taxes or payment fees exist.
"""

from __future__ import annotations

import csv
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

import database
import discounts
import permissions
import products
import receipt
from formatting import CURRENCY_CODE, format_money

BASE_DIR = Path(__file__).resolve().parent
REPORTS_DIR = BASE_DIR / "reports"

REPORT_WIDTH = 36
MAX_TREND_DAYS = 31
TOP_PRODUCTS = 5
STATUS_COMPLETED = "completed"
STATUS_VOIDED = "voided"
EPSILON = 1e-9


# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------
@contextmanager
def _read_snapshot(db_path: Any) -> Iterator[sqlite3.Connection]:
    """One consistent, read-only view of the database; never commits."""
    conn = database.get_connection(db_path)
    try:
        conn.execute("BEGIN")
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(row) for row in cursor.fetchall()]


def _money(value: Any) -> float:
    return round(float(value or 0.0) + EPSILON, 2)


def _count(value: Any) -> int:
    return int(float(value or 0))


def normalize_date(value: Any = None) -> str:
    """Return a strict YYYY-MM-DD string; None / blank means today (local).

    A full 'YYYY-MM-DD HH:MM:SS' database timestamp is accepted too - the
    first ten characters are used. Loose dates such as '2026-1-2' are
    rejected so a date picker can never silently query the wrong day.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return date.today().isoformat()
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    candidate = text[:10]
    try:
        parsed = datetime.strptime(candidate, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(
            "Enter a date as YYYY-MM-DD, for example 2026-11-12."
        ) from None
    if parsed.isoformat() != candidate:
        raise ValueError("Enter a date as YYYY-MM-DD, for example 2026-11-12.")
    return candidate


def shift_date(value: Any, days: int) -> str:
    """Move a date by whole days (used by the Previous / Next buttons)."""
    base = datetime.strptime(normalize_date(value), "%Y-%m-%d").date()
    return (base + timedelta(days=int(days))).isoformat()


def _rule() -> str:
    return "-" * REPORT_WIDTH


def _centre(text: str) -> str:
    return str(text).center(REPORT_WIDTH)


def _row(left: Any, right: Any) -> str:
    left_text = str(left)
    right_text = str(right)
    gap = REPORT_WIDTH - len(left_text) - len(right_text)
    if gap < 1:
        return left_text + "\n" + right_text.rjust(REPORT_WIDTH)
    return left_text + " " * gap + right_text


def _clip(text: Any) -> str:
    value = str(text or "-")
    return value if len(value) <= REPORT_WIDTH else value[: REPORT_WIDTH - 1] + "…"


def _wrap_text(text: Any, width: int = REPORT_WIDTH) -> list[str]:
    """Word-wrap free text (e.g. a void reason) without losing any words."""
    words = str(text or "").split()
    if not words:
        return ["-"]
    lines: list[str] = []
    current = ""
    for word in words:
        while len(word) > width:
            if current:
                lines.append(current)
                current = ""
            lines.append(word[:width])
            word = word[width:]
        if not word:
            continue
        candidate = f"{current} {word}".strip()
        if len(candidate) > width and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


# ----------------------------------------------------------------------
# Queries
# ----------------------------------------------------------------------
_DAY_SALES_SQL = """
    SELECT s.sale_id, s.transaction_number, s.transaction_date,
           s.item_count, s.total_amount, s.payment_method, s.status,
           s.voided_at, s.void_reason,
           s.subtotal_amount, s.discount_type, s.discount_rate,
           s.discount_amount, s.customer_name, s.customer_id,
           COALESCE(NULLIF(s.subtotal_amount, 0), s.total_amount)
               AS gross_amount,
           COALESCE(u.username, '-') AS cashier
    FROM store_sales AS s
    LEFT JOIN users AS u ON u.user_id = s.created_by
    WHERE substr(s.transaction_date, 1, 10) = ?
    ORDER BY s.sale_id
"""

_DAY_ITEMS_SQL = """
    SELECT i.sale_id, i.barcode, i.product_name, i.category,
           i.quantity, i.line_total
    FROM store_sale_items AS i
    JOIN store_sales AS s ON s.sale_id = i.sale_id
    WHERE substr(s.transaction_date, 1, 10) = ? AND s.status = ?
    ORDER BY i.sale_item_id
"""


def _bump(
    bucket: dict[str, dict[str, Any]],
    key: str,
    label: str,
    *,
    transactions: int = 0,
    quantity: int = 0,
    total: float = 0.0,
) -> None:
    entry = bucket.setdefault(
        key, {"name": label, "transactions": 0, "quantity": 0, "total": 0.0}
    )
    entry["transactions"] += transactions
    entry["quantity"] += quantity
    entry["total"] += total


def _finish(bucket: dict[str, dict[str, Any]], limit: int = 0) -> list[dict[str, Any]]:
    rows = []
    for entry in bucket.values():
        rows.append(
            {
                "name": entry["name"],
                "transactions": entry["transactions"],
                "quantity": entry["quantity"],
                "total": _money(entry["total"]),
            }
        )
    rows.sort(key=lambda row: (-row["total"], -row["quantity"], row["name"]))
    return rows[:limit] if limit else rows


# ----------------------------------------------------------------------
# Daily summary
# ----------------------------------------------------------------------
def daily_summary(
    date_value: Any = None,
    db_path: Any = None,
    actor: Any = None,
) -> dict[str, Any]:
    """Aggregate one business day. Manager only; never writes sales data."""
    permissions.require(actor, permissions.CAP_VIEW_REPORTS)
    day = normalize_date(date_value)
    products.ensure_schema(db_path)

    with _read_snapshot(db_path) as conn:
        sales = _rows(conn.execute(_DAY_SALES_SQL, (day,)))
        items = _rows(
            conn.execute(_DAY_ITEMS_SQL, (day, STATUS_COMPLETED))
        )

    completed = [row for row in sales if str(row["status"]) == STATUS_COMPLETED]
    voided = [row for row in sales if str(row["status"]) == STATUS_VOIDED]

    # Stage 15: gross is the PRE-discount subtotal (legacy rows fall back to
    # total_amount via gross_amount). Only completed sales contribute a
    # discount, because a voided sale's discount is reversed with the sale.
    def _gross(row: dict[str, Any]) -> float:
        return float(row.get("gross_amount") or 0.0)

    gross = _money(sum(_gross(row) for row in sales))
    voided_amount = _money(sum(_gross(row) for row in voided))
    discount_amount = _money(
        sum(float(row.get("discount_amount") or 0.0) for row in completed)
    )
    net = _money(gross - voided_amount - discount_amount)
    by_type: dict[str, dict[str, Any]] = {
        discounts.SENIOR: {"label": discounts.LABELS[discounts.SENIOR],
                          "count": 0, "total": 0.0},
        discounts.PWD: {"label": discounts.LABELS[discounts.PWD],
                        "count": 0, "total": 0.0},
    }
    discounted_count = 0
    for row in completed:
        amount = float(row.get("discount_amount") or 0.0)
        kind = str(row.get("discount_type") or "")
        if amount > 0 and kind in by_type:
            by_type[kind]["count"] += 1
            by_type[kind]["total"] = _money(by_type[kind]["total"] + amount)
            discounted_count += 1
    items_sold = sum(_count(row["item_count"]) for row in completed)
    average = _money(net / len(completed)) if completed else 0.0

    payments: dict[str, dict[str, Any]] = {}
    cashiers: dict[str, dict[str, Any]] = {}
    for row in completed:
        total = float(row["total_amount"] or 0.0)
        _bump(
            payments, str(row["payment_method"] or "Other"),
            str(row["payment_method"] or "Other"),
            transactions=1, total=total,
        )
        _bump(
            cashiers, str(row["cashier"] or "-"), str(row["cashier"] or "-"),
            transactions=1, total=total,
        )

    categories: dict[str, dict[str, Any]] = {}
    product_lines: dict[str, dict[str, Any]] = {}
    for item in items:
        quantity = _count(item["quantity"])
        total = float(item["line_total"] or 0.0)
        category = str(item["category"] or "Other")
        _bump(categories, category, category, quantity=quantity, total=total)
        product_key = f"{item['barcode']}|{item['product_name']}"
        _bump(
            product_lines, product_key, str(item["product_name"]),
            quantity=quantity, total=total,
        )

    products_ranked = sorted(
        _finish(product_lines),
        key=lambda row: (-row["quantity"], -row["total"], row["name"]),
    )[:TOP_PRODUCTS]

    transactions = [
        {
            "sale_id": int(row["sale_id"]),
            "transaction_number": str(row["transaction_number"]),
            "transaction_date": str(row["transaction_date"]),
            "cashier": str(row["cashier"]),
            "item_count": _count(row["item_count"]),
            "subtotal_amount": _money(_gross(row)),
            "total_amount": _money(row["total_amount"]),
            "discount_type": str(row.get("discount_type") or ""),
            "discount_label": discounts.LABELS.get(
                str(row.get("discount_type") or ""), ""
            ),
            "discount_amount": _money(row.get("discount_amount") or 0.0),
            "customer_name": str(row.get("customer_name") or ""),
            "customer_id": str(row.get("customer_id") or ""),
            "payment_method": str(row["payment_method"]),
            "status": str(row["status"]),
            "void_reason": str(row["void_reason"] or ""),
        }
        for row in reversed(sales)
    ]

    return {
        "date": day,
        "store": products.STORE_LABEL,
        "currency_code": CURRENCY_CODE,
        "generated_at": database.local_timestamp(),
        "gross_sales": gross,
        "voided_amount": voided_amount,
        "discount_amount": discount_amount,
        "discount_count": discounted_count,
        "discounts_by_type": by_type,
        "net_sales": net,
        "transaction_count": len(sales),
        "completed_count": len(completed),
        "voided_count": len(voided),
        "items_sold": items_sold,
        "average_basket": average,
        "first_sale": str(sales[0]["transaction_date"]) if sales else "",
        "last_sale": str(sales[-1]["transaction_date"]) if sales else "",
        "by_payment": _finish(payments),
        "by_cashier": _finish(cashiers),
        "by_category": _finish(categories),
        "top_products": products_ranked,
        "transactions": transactions,
    }


# ----------------------------------------------------------------------
# Multi-day trend
# ----------------------------------------------------------------------
def sales_trend(
    days: int = 7,
    end_date: Any = None,
    db_path: Any = None,
    actor: Any = None,
) -> list[dict[str, Any]]:
    """Last `days` days ending on `end_date`, including days with no sales."""
    permissions.require(actor, permissions.CAP_VIEW_REPORTS)
    try:
        span = int(days)
    except (TypeError, ValueError):
        raise ValueError("The trend length must be a whole number of days.") from None
    if span < 1 or span > MAX_TREND_DAYS:
        raise ValueError(f"Choose between 1 and {MAX_TREND_DAYS} days.")
    end = datetime.strptime(normalize_date(end_date), "%Y-%m-%d").date()
    start = end - timedelta(days=span - 1)
    products.ensure_schema(db_path)

    with _read_snapshot(db_path) as conn:
        rows = _rows(
            conn.execute(
                "SELECT substr(transaction_date, 1, 10) AS day, "
                "COUNT(*) AS transactions, "
                "COALESCE(SUM(total_amount), 0) AS total "
                "FROM store_sales "
                "WHERE status = ? AND substr(transaction_date, 1, 10) "
                "BETWEEN ? AND ? GROUP BY day",
                (STATUS_COMPLETED, start.isoformat(), end.isoformat()),
            )
        )
    by_day = {
        str(row["day"]): (_count(row["transactions"]), _money(row["total"]))
        for row in rows
    }
    trend = []
    for offset in range(span):
        day = (start + timedelta(days=offset)).isoformat()
        count, total = by_day.get(day, (0, 0.0))
        trend.append({"date": day, "transactions": count, "net_sales": total})
    return trend


# ----------------------------------------------------------------------
# Printable Z-read
# ----------------------------------------------------------------------
def build_z_read(summary: dict[str, Any]) -> str:
    """Fixed-width end-of-day report. Every amount uses format_money()."""
    lines: list[str] = [
        _centre(receipt.MART_NAME),
        _centre("END-OF-DAY Z-READ"),
        _rule(),
        _row("Date", str(summary.get("date") or "-")),
        _row("Generated", str(summary.get("generated_at") or "-")),
        _rule(),
        _row("Gross sales", format_money(summary.get("gross_sales"))),
        _row("Voids", format_money(summary.get("voided_amount"))),
        _row(
            "SC/PWD discounts",
            "-" + format_money(summary.get("discount_amount", 0.0)),
        ),
        _row("NET SALES", format_money(summary.get("net_sales"))),
        _rule(),
        _row("Transactions", str(summary.get("transaction_count", 0))),
        _row(
            "Completed / void",
            f"{summary.get('completed_count', 0)} / "
            f"{summary.get('voided_count', 0)}",
        ),
        _row("Items sold", str(summary.get("items_sold", 0))),
        _row("Average basket", format_money(summary.get("average_basket"))),
        _rule(),
        "SENIOR / PWD DISCOUNTS",
    ]
    breakdown = summary.get("discounts_by_type") or {}
    if float(summary.get("discount_amount") or 0.0) > 0:
        for kind in (discounts.SENIOR, discounts.PWD):
            entry = breakdown.get(kind) or {}
            if int(entry.get("count") or 0):
                lines.append(
                    _row(
                        _clip(entry.get("label", kind)),
                        f"{entry['count']} x {format_money(entry.get('total'))}",
                    )
                )
    else:
        lines.append("(no discounted sales)")

    lines.extend([_rule(), "PAYMENT MIX"])
    payments = list(summary.get("by_payment") or [])
    if payments:
        for row in payments:
            lines.append(_row(_clip(row["name"]), format_money(row["total"])))
    else:
        lines.append("(no completed sales)")

    lines.extend([_rule(), "CASHIER TOTALS"])
    cashiers = list(summary.get("by_cashier") or [])
    if cashiers:
        for row in cashiers:
            lines.append(_row(_clip(row["name"]), format_money(row["total"])))
    else:
        lines.append("(no completed sales)")

    lines.extend([_rule(), "TOP PRODUCTS"])
    top = list(summary.get("top_products") or [])
    if top:
        for row in top:
            lines.append(_clip(row["name"]))
            lines.append(
                _row(
                    f"  {row['quantity']} sold",
                    format_money(row["total"]),
                )
            )
    else:
        lines.append("(no completed sales)")

    voids = [
        row for row in (summary.get("transactions") or [])
        if row.get("status") == STATUS_VOIDED
    ]
    lines.extend([_rule(), _row("VOIDED SALES", str(len(voids)))])
    for row in voids[:5]:
        lines.append(_clip(row["transaction_number"]))
        lines.append("  Reason:")
        lines.extend(_wrap_text(row.get("void_reason") or "-"))

    lines.extend(
        [
            _rule(),
            _centre(f"All amounts in {summary.get('currency_code') or CURRENCY_CODE}"),
            _centre("End of Z-read  ·  Thank you!"),
            "",
        ]
    )
    return "\n".join(lines)


def archive_z_read(
    text: str, date_value: Any = None, base_dir: Any = None
) -> Path:
    """Write one UTF-8 Z-read into reports/ (or a test/temp directory)."""
    day = normalize_date(date_value)
    folder = Path(base_dir) if base_dir is not None else REPORTS_DIR
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"Z-READ-{day}.txt"
    target.write_text(text, encoding="utf-8")
    return target


def issue_z_read(
    summary: dict[str, Any],
    *,
    auto_print: bool = True,
    base_dir: Any = None,
    printer_name: str | None = None,
    actor: Any = None,
) -> dict[str, Any]:
    """Archive + best-effort print. Printing never affects the database."""
    permissions.require(actor, permissions.CAP_VIEW_REPORTS)
    text = build_z_read(summary)
    path = archive_z_read(text, summary.get("date"), base_dir)
    if auto_print:
        printed = receipt.print_receipt(
            text, path=path, printer_name=printer_name
        )
    else:
        printed = {
            "method": "skipped",
            "detail": "Printing skipped; Z-read archived.",
            "ok": True,
        }
    return {"text": text, "path": path, "print": printed}


# ----------------------------------------------------------------------
# CSV export (Excel-friendly UTF-8 with BOM)
# ----------------------------------------------------------------------
def export_transactions_csv(
    date_value: Any = None,
    *,
    base_dir: Any = None,
    db_path: Any = None,
    actor: Any = None,
) -> Path:
    """Write the day's transactions to reports/sales-YYYY-MM-DD.csv."""
    summary = daily_summary(date_value, db_path=db_path, actor=actor)
    folder = Path(base_dir) if base_dir is not None else REPORTS_DIR
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"sales-{summary['date']}.csv"
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "Transaction no.", "Date / time", "Cashier", "Items",
                f"Total ({CURRENCY_CODE})", "Payment", "Status", "Void reason",
                # Stage 15 - keep the original eight columns in place so old
                # spreadsheets still import; discount detail is appended.
                f"Subtotal ({CURRENCY_CODE})", "Discount type",
                f"Discount ({CURRENCY_CODE})", "Customer name", "ID number",
            ]
        )
        for row in summary["transactions"]:
            writer.writerow(
                [
                    row["transaction_number"],
                    row["transaction_date"],
                    row["cashier"],
                    row["item_count"],
                    f"{row['total_amount']:.2f}",
                    row["payment_method"],
                    row["status"],
                    row["void_reason"],
                    f"{row['subtotal_amount']:.2f}",
                    # daily_summary always fills this; a plain sale is spelled
                    # out as "No discount" so the column is never ambiguous.
                    row["discount_label"] or "No discount",
                    f"{row['discount_amount']:.2f}",
                    row["customer_name"] or "-",
                    row["customer_id"] or "-",
                ]
            )
    return target
