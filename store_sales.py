"""
Stage 12 - convenience-store Point of Sale data layer.

A sale is a CART of barcode-scanned products (not a number of ramen servings).
The cashier scans a product, it is added to the cart, and ONE confirmation
commits the whole basket:

    BEGIN IMMEDIATE
      1. re-resolve every barcode to a live, sellable product
      2. re-check stock for EVERY line (no partial deductions)
      3. write one stock-out header + one detail line per product
      4. update each product's quantity_on_hand
      5. write store_sales + immutable store_sale_items snapshots
    COMMIT
    archive + print the receipt (AFTER the commit)

Why this shape:
  * Every deduction is an ordinary stock-out line, so the existing
    vw_stock_movements view, Dashboard, History and Monitoring screens show
    store sales automatically - no changes to those queries.
  * Money, name, barcode, unit and balances are SNAPSHOTS. Renaming or
    repricing a product later never rewrites an old receipt.
  * A cancelled basket (before Confirm) writes absolutely nothing. A sale
    voided AFTER Confirm keeps its history and gets a compensating stock-in.
  * Re-checking inside BEGIN IMMEDIATE prevents two windows selling the last
    item at the same time.

All money is Philippine peso (PHP / ₱).
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Any, Iterable, Iterator

import database
import permissions
import products
from formatting import CURRENCY_CODE

EPSILON = 1e-9
SALE_REASON = "Sale"
STATUS_COMPLETED = "completed"
STATUS_VOIDED = "voided"
MAX_LINES = 60
MAX_QUANTITY = 999


# ----------------------------------------------------------------------
# Connection helpers
# ----------------------------------------------------------------------
@contextmanager
def _read_snapshot(db_path: Any) -> Iterator[sqlite3.Connection]:
    """Consistent read-only view; never commits."""
    conn = database.get_connection(db_path)
    try:
        conn.execute("BEGIN")
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(row) for row in cursor.fetchall()]


def _money(value: float) -> float:
    return round(float(value) + EPSILON, 2)


def _balance(value: float) -> float:
    return round(float(value), 4)


def _resolve_user_id(conn: sqlite3.Connection, user_id: Any) -> int | None:
    if user_id is None:
        return None
    try:
        candidate = int(user_id)
    except (TypeError, ValueError):
        return None
    row = conn.execute(
        "SELECT user_id FROM users WHERE user_id = ?", (candidate,)
    ).fetchone()
    return candidate if row else None


def _username(conn: sqlite3.Connection, user_id: int | None) -> str:
    if user_id is None:
        return "—"
    row = conn.execute(
        "SELECT username FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    return str(row[0]) if row else "—"


# ----------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------
def validate_payment_method(value: Any) -> str:
    method = str(value if value is not None else "").strip()
    if not method:
        raise ValueError("Choose a payment method.")
    if method not in products.PAYMENT_METHODS:
        raise ValueError("Choose a valid payment method.")
    return method


def validate_tendered(value: Any) -> float:
    return products.validate_money(value, "Amount tendered")


def validate_void_reason(value: Any) -> str:
    reason = str(value if value is not None else "").strip()
    if len(reason) < 3:
        raise ValueError("Give a short reason for voiding this sale.")
    if len(reason) > 120:
        raise ValueError("The void reason is too long.")
    return reason


def normalize_cart(cart: Any) -> list[dict[str, Any]]:
    """Accept [(barcode, qty), ...] or [{'barcode':..., 'quantity':...}, ...].

    Duplicate barcodes are merged, so scanning the same product three times
    becomes one line with quantity 3. Raises ValueError for a bad basket.
    """
    if cart is None:
        raise ValueError("Scan a product before confirming the sale.")
    if isinstance(cart, (str, bytes, dict)):
        raise ValueError("The cart must be a list of scanned products.")
    merged: dict[str, int] = {}
    try:
        entries: Iterable[Any] = list(cart)
    except TypeError:
        raise ValueError("The cart must be a list of scanned products.") from None
    for entry in entries:
        if isinstance(entry, dict):
            code = entry.get("barcode")
            quantity = entry.get("quantity", 1)
        elif isinstance(entry, (tuple, list)) and len(entry) == 2:
            code, quantity = entry
        else:
            raise ValueError("Each cart line needs a barcode and a quantity.")
        clean = str(code if code is not None else "").strip()
        if not clean:
            raise ValueError("A cart line has no barcode.")
        count = products.validate_whole(quantity, "Quantity", allow_zero=False)
        if count > MAX_QUANTITY:
            raise ValueError("A single line cannot exceed 999 units.")
        merged[clean] = merged.get(clean, 0) + count
        if merged[clean] > MAX_QUANTITY:
            raise ValueError("A single line cannot exceed 999 units.")
    if not merged:
        raise ValueError("Scan a product before confirming the sale.")
    if len(merged) > MAX_LINES:
        raise ValueError("A sale cannot have more than 60 different products.")
    return [
        {"barcode": code, "quantity": count} for code, count in merged.items()
    ]


def _resolve_lines(
    conn: sqlite3.Connection, cart_lines: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], float, list[str]]:
    """Resolve barcodes to live products and price the basket."""
    lines: list[dict[str, Any]] = []
    total = 0.0
    shortages: list[str] = []
    for entry in cart_lines:
        code = entry["barcode"]
        quantity = int(entry["quantity"])
        row = conn.execute(
            "SELECT ingredient_id, name, barcode, category, unit, unit_price, "
            "quantity_on_hand, is_active, is_sellable "
            "FROM ingredients WHERE barcode = ?",
            (code,),
        ).fetchone()
        if row is None:
            raise ValueError(f"No product found for barcode {code}.")
        product = dict(row)
        if int(product["is_active"]) != 1 or int(product["is_sellable"]) != 1:
            raise ValueError(
                f"{product['name']} is retired and can no longer be sold."
            )
        price = _money(float(product["unit_price"]))
        if price <= 0:
            raise ValueError(
                f"Set a selling price for {product['name']} before selling it."
            )
        on_hand = float(product["quantity_on_hand"])
        line_total = _money(price * quantity)
        balance_after = _balance(on_hand - quantity)
        if quantity > on_hand + EPSILON:
            shortages.append(
                f"{product['name']}: only {products_quantity(on_hand, product['unit'])} "
                f"available, {quantity} requested"
            )
        lines.append(
            {
                "ingredient_id": int(product["ingredient_id"]),
                "barcode": str(product["barcode"]),
                "product_name": str(product["name"]),
                "category": str(product["category"]),
                "unit": str(product["unit"]),
                "quantity": quantity,
                "unit_price": price,
                "line_total": line_total,
                "balance_before": _balance(on_hand),
                "balance_after": balance_after,
            }
        )
        total += line_total
    return lines, _money(total), shortages


def products_quantity(value: float, unit: str) -> str:
    """Short stock warning text, e.g. '3 packs' (no numeric format specs)."""
    from formatting import format_quantity

    return format_quantity(value, unit)


def _next_transaction_number(
    conn: sqlite3.Connection, timestamp: str
) -> str:
    day = str(timestamp)[:10].replace("-", "")
    row = conn.execute(
        "SELECT COALESCE(MAX(sale_id), 0) + 1 FROM store_sales"
    ).fetchone()
    sequence = int(row[0])
    return f"SM-{day}-{sequence:04d}"


# ----------------------------------------------------------------------
# Preview (READ ONLY - no writes of any kind)
# ----------------------------------------------------------------------
def preview_sale(
    cart: Any, db_path: Any = None
) -> dict[str, Any]:
    """Price a basket without touching the database."""
    products.ensure_schema(db_path)
    cart_lines = normalize_cart(cart)
    with _read_snapshot(db_path) as conn:
        lines, total, shortages = _resolve_lines(conn, cart_lines)
    return {
        "items": lines,
        "item_count": sum(int(line["quantity"]) for line in lines),
        "line_count": len(lines),
        "total_amount": total,
        "currency_code": CURRENCY_CODE,
        "shortages": shortages,
        "can_confirm": not shortages,
    }


def lookup_product(barcode: Any, db_path: Any = None) -> dict[str, Any]:
    """Scanner lookup with a friendly error when the code is unknown."""
    products.ensure_schema(db_path)
    clean = str(barcode if barcode is not None else "").strip()
    if not clean:
        raise ValueError("Scan or type a barcode first.")
    match = products.find_by_barcode(clean, db_path)
    if match is None:
        raise ValueError(f"No sellable product found for barcode {clean}.")
    return match


# ----------------------------------------------------------------------
# Commit
# ----------------------------------------------------------------------
def confirm_sale(
    cart: Any,
    payment_method: Any,
    amount_tendered: Any,
    user_id: Any = None,
    db_path: Any = None,
    actor: Any = None,
) -> dict[str, Any]:
    """Commit one basket atomically. Returns everything the receipt needs."""
    permissions.require(actor, permissions.CAP_RECORD_SALE)
    products.ensure_schema(db_path)
    cart_lines = normalize_cart(cart)
    method = validate_payment_method(payment_method)
    tendered = validate_tendered(amount_tendered)
    timestamp = database.local_timestamp()

    with database.transaction(db_path) as conn:
        # Re-resolve INSIDE the write transaction: a manager may have retired
        # or repriced a product between the preview and the confirm.
        lines, total, shortages = _resolve_lines(conn, cart_lines)
        if shortages:
            raise ValueError("Not enough stock: " + "; ".join(shortages))
        if tendered + EPSILON < total:
            raise ValueError(
                "Amount tendered is less than the total. "
                "A price may have changed - check the basket again."
            )
        change = _money(tendered - total)

        created_by = _resolve_user_id(conn, user_id)
        cashier = _username(conn, created_by)
        number = _next_transaction_number(conn, timestamp)

        # 1 - Ordinary stock-out so Dashboard / History / Monitoring see it.
        header = conn.execute(
            """
            INSERT INTO stock_out_transactions
                (transaction_date, reason, notes, created_by)
            VALUES (?, ?, ?, ?)
            """,
            (timestamp, SALE_REASON, f"POS sale {number}", created_by),
        )
        stock_out_id = int(header.lastrowid)

        for line in lines:
            conn.execute(
                """
                INSERT INTO stock_out_details
                    (stock_out_id, ingredient_id, quantity, unit, notes)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    stock_out_id, line["ingredient_id"], line["quantity"],
                    line["unit"], f"POS sale {number}",
                ),
            )
            conn.execute(
                "UPDATE ingredients SET quantity_on_hand = ?, updated_at = ? "
                "WHERE ingredient_id = ?",
                (line["balance_after"], timestamp, line["ingredient_id"]),
            )

        # 2 - Sale header with money snapshots.
        sale_header = conn.execute(
            """
            INSERT INTO store_sales
                (transaction_number, transaction_date, item_count,
                 total_amount, currency_code, payment_method,
                 amount_tendered, change_amount, status,
                 stock_out_id, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                number, timestamp, sum(int(line["quantity"]) for line in lines),
                total, CURRENCY_CODE, method, tendered, change,
                STATUS_COMPLETED, stock_out_id, created_by,
            ),
        )
        sale_id = int(sale_header.lastrowid)

        # 3 - Immutable per-product snapshots for receipts and voids.
        for line in lines:
            conn.execute(
                """
                INSERT INTO store_sale_items
                    (sale_id, ingredient_id, barcode, product_name, category,
                     unit, quantity, unit_price, line_total,
                     balance_before, balance_after)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sale_id, line["ingredient_id"], line["barcode"],
                    line["product_name"], line["category"], line["unit"],
                    line["quantity"], line["unit_price"], line["line_total"],
                    line["balance_before"], line["balance_after"],
                ),
            )

    return {
        "sale_id": sale_id,
        "transaction_number": number,
        "transaction_date": timestamp,
        "store": products.STORE_LABEL,
        "item_count": sum(int(line["quantity"]) for line in lines),
        "line_count": len(lines),
        "items": lines,
        "total_amount": total,
        "currency_code": CURRENCY_CODE,
        "payment_method": method,
        "amount_tendered": tendered,
        "change_amount": change,
        "status": STATUS_COMPLETED,
        "cashier": cashier,
        "stock_out_id": stock_out_id,
    }


# ----------------------------------------------------------------------
# Reads
# ----------------------------------------------------------------------
def get_sales_history(
    search: str = "", limit: int = 100, db_path: Any = None
) -> list[dict[str, Any]]:
    products.ensure_schema(db_path)
    needle = str(search or "").strip()
    params: list[Any] = []
    where = ""
    if needle:
        where = (
            "WHERE s.transaction_number LIKE ? OR s.transaction_date LIKE ? "
            "OR EXISTS (SELECT 1 FROM store_sale_items i "
            "           WHERE i.sale_id = s.sale_id AND i.product_name LIKE ?)"
        )
        params = [f"%{needle}%"] * 3
    params.append(max(1, min(int(limit), 500)))
    conn = database.get_connection(db_path)
    try:
        cursor = conn.execute(
            "SELECT s.sale_id, s.transaction_number, s.transaction_date, "
            "s.item_count, s.total_amount, s.payment_method, s.status, "
            "COALESCE(u.username, '—') AS cashier "
            "FROM store_sales AS s "
            "LEFT JOIN users AS u ON u.user_id = s.created_by "
            f"{where} ORDER BY s.sale_id DESC LIMIT ?",
            params,
        )
        return _rows(cursor)
    finally:
        conn.close()


def get_sale_detail(sale_id: Any, db_path: Any = None) -> dict[str, Any]:
    products.ensure_schema(db_path)
    target = int(sale_id)
    conn = database.get_connection(db_path)
    try:
        header = _rows(conn.execute(
            "SELECT s.*, COALESCE(u.username, '—') AS cashier "
            "FROM store_sales AS s "
            "LEFT JOIN users AS u ON u.user_id = s.created_by "
            "WHERE s.sale_id = ?",
            (target,),
        ))
        if not header:
            raise ValueError("That sale does not exist.")
        sale = header[0]
        sale["items"] = _rows(conn.execute(
            "SELECT * FROM store_sale_items WHERE sale_id = ? "
            "ORDER BY sale_item_id",
            (target,),
        ))
        return sale
    finally:
        conn.close()


def gross_sales(db_path: Any = None) -> float:
    """Manager-only summary shown on the POS screen (never to a cashier)."""
    products.ensure_schema(db_path)
    conn = database.get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(total_amount), 0) FROM store_sales "
            "WHERE status = 'completed'"
        ).fetchone()
        return _money(float(row[0]))
    finally:
        conn.close()


# ----------------------------------------------------------------------
# Void (manager only) - immutable history + compensating stock-in
# ----------------------------------------------------------------------
def void_sale(
    sale_id: Any,
    reason: Any,
    user_id: Any = None,
    db_path: Any = None,
    actor: Any = None,
) -> dict[str, Any]:
    permissions.require(actor, permissions.CAP_VOID_SALE)
    products.ensure_schema(db_path)
    target = int(sale_id)
    clean_reason = validate_void_reason(reason)
    timestamp = database.local_timestamp()

    with database.transaction(db_path) as conn:
        header = conn.execute(
            "SELECT status FROM store_sales WHERE sale_id = ?", (target,)
        ).fetchone()
        if header is None:
            raise ValueError("That sale does not exist.")
        if str(header[0]) == STATUS_VOIDED:
            raise ValueError("That sale is already voided.")
        items = _rows(conn.execute(
            "SELECT * FROM store_sale_items WHERE sale_id = ? "
            "ORDER BY sale_item_id",
            (target,),
        ))
        voided_by = _resolve_user_id(conn, user_id)

        # Additive restoration (not an absolute balance): other stock
        # movements may have happened since the sale was committed.
        in_header = conn.execute(
            """
            INSERT INTO stock_in_transactions
                (transaction_date, reference_no, notes, created_by)
            VALUES (?, ?, ?, ?)
            """,
            (timestamp, "VOID", f"Reversal of voided POS sale #{target}", voided_by),
        )
        stock_in_id = int(in_header.lastrowid)
        for item in items:
            quantity = float(item["quantity"])
            conn.execute(
                """
                INSERT INTO stock_in_details
                    (stock_in_id, ingredient_id, quantity, unit,
                     expiration_date, notes)
                VALUES (?, ?, ?, ?, NULL, ?)
                """,
                (
                    stock_in_id, int(item["ingredient_id"]), quantity,
                    str(item["unit"]), f"Voided sale #{target}",
                ),
            )
            conn.execute(
                "UPDATE ingredients SET quantity_on_hand = quantity_on_hand + ?, "
                "updated_at = ? WHERE ingredient_id = ?",
                (quantity, timestamp, int(item["ingredient_id"])),
            )

        conn.execute(
            "UPDATE store_sales SET status = ?, voided_at = ?, voided_by = ?, "
            "void_reason = ? WHERE sale_id = ?",
            (STATUS_VOIDED, timestamp, voided_by, clean_reason, target),
        )

    return get_sale_detail(target, db_path)
