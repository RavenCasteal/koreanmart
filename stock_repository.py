"""
Stage 5 - stock movement data access layer.

Everything about physical stock lives here so the user-interface files never
contain SQL. A transaction is a HEADER plus one or more DETAIL lines, exactly
as the schema was designed in Stage 1.

The three rules this module enforces:
  1. quantity_on_hand may ONLY change through record_stock_in() /
     record_stock_out(). There is no "edit balance" function anywhere.
  2. All lines of one transaction are written atomically: either every
     balance, header and detail is committed, or nothing is. A failed
     stock-out never leaves a half-finished transaction behind.
  3. A stock-out can never make a balance negative. The check happens INSIDE
     the BEGIN IMMEDIATE transaction, and the schema's CHECK constraint is a
     second line of defence.

Recipe amounts (recipe_ingredients) and the recipe header are never touched:
serving a bowl does not rewrite the recipe, and receiving a delivery does not
change how much one serving needs.
"""

from __future__ import annotations

import math
import re
import sqlite3
from datetime import datetime
from typing import Any

import database
import permissions
import repository
from formatting import format_quantity

# Validation limits. Keeping them together makes the UI hints consistent.
MAX_QUANTITY = 1_000_000.0
MAX_NOTES_LENGTH = 200
MAX_SHORT_TEXT_LENGTH = 60
MAX_LINES_PER_TRANSACTION = 12
HISTORY_ROW_LIMIT = 250

# Suggested reasons for the MANUAL stock-out combo box; free text is allowed.
# "Sale" is deliberately NOT offered here: a sale stock-out must only be
# created by sales_repository.confirm_sale(), which also writes the matching
# sales_transactions / sale_deductions rows. Otherwise a cashier could remove
# stock labelled "Sale" with no sale record, breaking the audit trail.
STOCK_OUT_REASONS: tuple[str, ...] = (
    "Consumption",
    "Expired",
    "Damaged",
    "Manual correction",
)

_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------
def _resolve_path(db_path: Any) -> Any:
    """Use the application database unless a test supplies a temp path."""
    return database.DB_PATH if db_path is None else db_path


def _rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    """Return plain dicts regardless of the connection's row_factory."""
    columns = [description[0] for description in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _balance_after(balance: float, change: float) -> float:
    """Add or subtract stock without float dust such as 0.30000000000000004."""
    return max(0.0, round(balance + change, 6))


# ----------------------------------------------------------------------
# Validation (every function raises ValueError with a friendly message)
# ----------------------------------------------------------------------
def validate_quantity(value: Any) -> float:
    text = str(value if value is not None else "").strip()
    if not text:
        raise ValueError("Quantity is required.")
    try:
        number = float(text)
    except ValueError as exc:
        raise ValueError("Quantity must be a number, e.g. 150 or 2.5.") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError("Quantity must be greater than zero.")
    if number > MAX_QUANTITY:
        raise ValueError(f"Quantity must be {MAX_QUANTITY:g} or less.")
    return number


def validate_notes(value: Any) -> str:
    notes = str(value if value is not None else "").strip()
    if len(notes) > MAX_NOTES_LENGTH:
        raise ValueError(f"Notes must be {MAX_NOTES_LENGTH} characters or fewer.")
    return notes


def validate_reference(value: Any) -> str:
    """Optional delivery reference / invoice number."""
    reference = str(value if value is not None else "").strip()
    if len(reference) > MAX_SHORT_TEXT_LENGTH:
        raise ValueError(
            f"Reference number must be {MAX_SHORT_TEXT_LENGTH} characters or fewer."
        )
    return reference


def validate_reason(value: Any) -> str:
    """Stock-out header reason is required by the schema's CHECK constraint."""
    reason = str(value if value is not None else "").strip()
    if not reason:
        raise ValueError("A reason for the stock-out is required.")
    if len(reason) > MAX_SHORT_TEXT_LENGTH:
        raise ValueError(
            f"Reason must be {MAX_SHORT_TEXT_LENGTH} characters or fewer."
        )
    return reason


def validate_expiry(value: Any) -> str | None:
    """Optional per-delivery expiry date, stored on the stock-in detail line."""
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    if not _DATE_PATTERN.match(text):
        raise ValueError("Expiration date must use the format YYYY-MM-DD.")
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("Expiration date is not a real calendar date.") from exc
    return parsed.isoformat()


def _validate_lines(lines: Any, *, with_expiry: bool) -> list[dict[str, Any]]:
    """Validate and normalise every detail line BEFORE opening a transaction."""
    if not isinstance(lines, (list, tuple)) or not lines:
        raise ValueError("Add at least one ingredient line.")
    if len(lines) > MAX_LINES_PER_TRANSACTION:
        raise ValueError(
            f"A transaction can hold at most {MAX_LINES_PER_TRANSACTION} lines. "
            "Save this one and start another."
        )

    clean: list[dict[str, Any]] = []
    seen: set[int] = set()
    for index, raw in enumerate(lines, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"Line {index} is not a valid ingredient line.")
        try:
            ingredient_id = int(raw.get("ingredient_id"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Line {index}: please choose an ingredient.") from exc
        if ingredient_id in seen:
            raise ValueError(
                "Each ingredient may appear only once per transaction. "
                "Combine the amounts into one line instead."
            )
        seen.add(ingredient_id)

        try:
            quantity = validate_quantity(raw.get("quantity"))
        except ValueError as exc:
            raise ValueError(f"Line {index}: {exc}") from exc
        try:
            line_notes = validate_notes(raw.get("notes"))
        except ValueError as exc:
            raise ValueError(f"Line {index}: {exc}") from exc
        try:
            expiry = (
                validate_expiry(raw.get("expiration_date")) if with_expiry else None
            )
        except ValueError as exc:
            raise ValueError(f"Line {index}: {exc}") from exc

        clean.append(
            {
                "ingredient_id": ingredient_id,
                "quantity": quantity,
                "expiration_date": expiry,
                "notes": line_notes,
            }
        )
    return clean


def _ingredient_for_movement(
    conn: sqlite3.Connection, ingredient_id: int
) -> dict[str, Any]:
    """Re-check the catalog INSIDE the write transaction (no stale balances)."""
    row = conn.execute(
        """
        SELECT name, unit, quantity_on_hand, is_active, expiration_date
        FROM ingredients WHERE ingredient_id = ?
        """,
        (ingredient_id,),
    ).fetchone()
    if row is None or row[3] != 1:
        raise ValueError(
            "One of the chosen ingredients is no longer active. "
            "Close this form and open it again."
        )
    return {
        "name": str(row[0]),
        "unit": str(row[1]),
        "quantity_on_hand": float(row[2]),
        "expiration_date": row[4],
    }


def _resolve_user_id(conn: sqlite3.Connection, user_id: Any) -> int | None:
    """Only a real, active administrator may be recorded as created_by."""
    if user_id is None:
        return None
    try:
        candidate = int(user_id)
    except (TypeError, ValueError):
        return None
    exists = conn.execute(
        "SELECT 1 FROM users WHERE user_id = ? AND is_active = 1", (candidate,)
    ).fetchone()
    return candidate if exists else None


# ----------------------------------------------------------------------
# Reads
# ----------------------------------------------------------------------
def list_stock_ingredients(db_path: Any = None) -> list[dict[str, Any]]:
    """Active ingredients with live balances (reuses Stage 3's catalog query)."""
    return repository.list_ingredients("", db_path=_resolve_path(db_path))


def get_inventory_summary(db_path: Any = None) -> dict[str, Any]:
    """Every number shown on the Inventory page, read from SQLite."""
    with database.transaction(_resolve_path(db_path)) as conn:
        active = conn.execute(
            "SELECT COUNT(*) FROM ingredients WHERE is_active = 1"
        ).fetchone()[0]
        with_stock = conn.execute(
            """
            SELECT COUNT(*) FROM ingredients
            WHERE is_active = 1 AND quantity_on_hand > 0
            """
        ).fetchone()[0]
        low_stock = conn.execute(
            """
            SELECT COUNT(*) FROM ingredients
            WHERE is_active = 1 AND minimum_stock > 0
              AND quantity_on_hand > 0 AND quantity_on_hand <= minimum_stock
            """
        ).fetchone()[0]
        out_of_stock = conn.execute(
            """
            SELECT COUNT(*) FROM ingredients
            WHERE is_active = 1 AND quantity_on_hand = 0
            """
        ).fetchone()[0]
        stock_in_lines = conn.execute(
            "SELECT COUNT(*) FROM stock_in_details"
        ).fetchone()[0]
        stock_out_lines = conn.execute(
            "SELECT COUNT(*) FROM stock_out_details"
        ).fetchone()[0]
        last = conn.execute(
            "SELECT MAX(transaction_date) FROM vw_stock_movements"
        ).fetchone()[0]

    return {
        "active_ingredients": active,
        "with_stock": with_stock,
        "low_stock": low_stock,
        "out_of_stock": out_of_stock,
        "stock_in_lines": stock_in_lines,
        "stock_out_lines": stock_out_lines,
        "total_movements": stock_in_lines + stock_out_lines,
        "last_movement": last or "—",
    }


def _movement_where(
    movement_type: str, search: str
) -> tuple[str, list[Any]]:
    """Shared WHERE clause for the history table and its count."""
    kind = str(movement_type or "ALL").upper()
    if kind not in ("ALL", "IN", "OUT"):
        raise ValueError("Movement type must be All, IN or OUT.")

    conditions: list[str] = []
    params: list[Any] = []
    if kind != "ALL":
        conditions.append("movement_type = ?")
        params.append(kind)

    keyword = str(search or "").strip()
    if keyword:
        escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conditions.append(
            "("
            "ingredient_name LIKE ? ESCAPE '\\' OR "
            "reason LIKE ? ESCAPE '\\' OR "
            "notes LIKE ? ESCAPE '\\' OR "
            "transaction_date LIKE ? ESCAPE '\\' OR "
            "unit LIKE ? ESCAPE '\\'"
            ")"
        )
        params.extend([f"%{escaped}%"] * 5)

    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    return where, params


def get_history(
    movement_type: str = "ALL",
    search: str = "",
    limit: int = HISTORY_ROW_LIMIT,
    db_path: Any = None,
) -> dict[str, Any]:
    """One consistent snapshot for the History screen.

    Returns filtered rows plus global totals, so summary cards never disagree
    with the table. Stock-out quantities stay NEGATIVE, as in the SQL view.
    """
    where, params = _movement_where(movement_type, search)
    cap = max(1, min(int(limit), 1000))
    with database.transaction(_resolve_path(db_path)) as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM vw_stock_movements"
        ).fetchone()[0]
        filtered = conn.execute(
            f"SELECT COUNT(*) FROM vw_stock_movements {where}", params
        ).fetchone()[0]
        in_lines = conn.execute(
            "SELECT COUNT(*) FROM vw_stock_movements WHERE movement_type = 'IN'"
        ).fetchone()[0]
        out_lines = total - in_lines
        last = conn.execute(
            "SELECT MAX(transaction_date) FROM vw_stock_movements"
        ).fetchone()[0]
        rows = _rows(
            conn.execute(
                f"""
                SELECT movement_type, transaction_id, detail_id,
                       transaction_date, ingredient_name, unit, quantity,
                       reason, notes, created_by
                FROM vw_stock_movements
                {where}
                ORDER BY transaction_date DESC, detail_id DESC
                LIMIT ?
                """,
                (*params, cap),
            )
        )
    return {
        "rows": rows,
        "total": total,
        "filtered": filtered,
        "stock_in_lines": in_lines,
        "stock_out_lines": out_lines,
        "last_movement": last or "—",
        "limit": cap,
    }


def get_recent_movements(limit: int = 5, db_path: Any = None) -> list[dict[str, Any]]:
    """Small 'latest activity' list shown on the Inventory page."""
    return get_history("ALL", "", limit, db_path)["rows"]


def get_movement_detail(
    movement_type: str, transaction_id: int, detail_id: int, db_path: Any = None
) -> dict[str, Any]:
    """Full header + detail information for one history row."""
    kind = str(movement_type or "").upper()
    if kind == "IN":
        sql = """
            SELECT 'IN' AS movement_type,
                   h.stock_in_id AS transaction_id,
                   d.stock_in_detail_id AS detail_id,
                   h.transaction_date AS transaction_date,
                   h.reference_no AS reference_no,
                   NULL AS reason,
                   COALESCE(h.notes, '') AS notes,
                   u.username AS created_by,
                   i.name AS ingredient_name,
                   d.unit AS unit,
                   d.quantity AS quantity,
                   d.expiration_date AS expiration_date,
                   COALESCE(d.notes, '') AS line_notes
            FROM stock_in_transactions AS h
            JOIN stock_in_details AS d ON d.stock_in_id = h.stock_in_id
            JOIN ingredients AS i ON i.ingredient_id = d.ingredient_id
            LEFT JOIN users AS u ON u.user_id = h.created_by
            WHERE h.stock_in_id = ? AND d.stock_in_detail_id = ?
        """
    elif kind == "OUT":
        sql = """
            SELECT 'OUT' AS movement_type,
                   h.stock_out_id AS transaction_id,
                   d.stock_out_detail_id AS detail_id,
                   h.transaction_date AS transaction_date,
                   NULL AS reference_no,
                   h.reason AS reason,
                   COALESCE(h.notes, '') AS notes,
                   u.username AS created_by,
                   i.name AS ingredient_name,
                   d.unit AS unit,
                   -d.quantity AS quantity,
                   NULL AS expiration_date,
                   COALESCE(d.notes, '') AS line_notes
            FROM stock_out_transactions AS h
            JOIN stock_out_details AS d ON d.stock_out_id = h.stock_out_id
            JOIN ingredients AS i ON i.ingredient_id = d.ingredient_id
            LEFT JOIN users AS u ON u.user_id = h.created_by
            WHERE h.stock_out_id = ? AND d.stock_out_detail_id = ?
        """
    else:
        raise ValueError("Unknown movement type.")

    with database.transaction(_resolve_path(db_path)) as conn:
        row = conn.execute(sql, (transaction_id, detail_id)).fetchone()
    if row is None:
        raise ValueError("That movement no longer exists.")
    return dict(row)


# ----------------------------------------------------------------------
# Writes - one database transaction per business transaction
# ----------------------------------------------------------------------
def record_stock_in(
    lines: Any,
    reference_no: Any = "",
    notes: Any = "",
    user_id: Any = None,
    db_path: Any = None,
    actor: Any = None,
) -> dict[str, Any]:
    """Record a delivery: add stock, write the header + detail lines.

    When a line carries an expiry date the ingredient's single expiry field is
    set to the EARLIEST of its current value and the delivered date, so the
    Monitoring screen stays conservative. Per-batch expiry tracking is a known
    limitation (see the stage report).
    """
    permissions.require(actor, permissions.CAP_MANAGE_STOCK)
    clean_lines = _validate_lines(lines, with_expiry=True)
    reference = validate_reference(reference_no)
    header_notes = validate_notes(notes)
    timestamp = database.local_timestamp()

    with database.transaction(_resolve_path(db_path)) as conn:
        ingredients = [
            _ingredient_for_movement(conn, line["ingredient_id"])
            for line in clean_lines
        ]
        created_by = _resolve_user_id(conn, user_id)
        cursor = conn.execute(
            """
            INSERT INTO stock_in_transactions
                (transaction_date, reference_no, notes, created_by)
            VALUES (?, ?, ?, ?)
            """,
            (timestamp, reference or None, header_notes or None, created_by),
        )
        transaction_id = int(cursor.lastrowid)

        result_lines: list[dict[str, Any]] = []
        for line, ingredient in zip(clean_lines, ingredients):
            conn.execute(
                """
                INSERT INTO stock_in_details
                    (stock_in_id, ingredient_id, quantity, unit,
                     expiration_date, notes)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    transaction_id,
                    line["ingredient_id"],
                    line["quantity"],
                    ingredient["unit"],
                    line["expiration_date"],
                    line["notes"] or None,
                ),
            )
            new_balance = _balance_after(
                ingredient["quantity_on_hand"], line["quantity"]
            )
            current_expiry = ingredient["expiration_date"]
            delivered_expiry = line["expiration_date"]
            if delivered_expiry is None:
                new_expiry = current_expiry
            elif current_expiry is None:
                new_expiry = delivered_expiry
            else:
                # ISO YYYY-MM-DD strings sort chronologically as text.
                new_expiry = min(current_expiry, delivered_expiry)
            conn.execute(
                """
                UPDATE ingredients
                SET quantity_on_hand = ?, expiration_date = ?, updated_at = ?
                WHERE ingredient_id = ?
                """,
                (new_balance, new_expiry, timestamp, line["ingredient_id"]),
            )
            result_lines.append(
                {
                    "name": ingredient["name"],
                    "quantity": line["quantity"],
                    "unit": ingredient["unit"],
                    "new_balance": new_balance,
                }
            )

    return {
        "movement_type": "IN",
        "transaction_id": transaction_id,
        "transaction_date": timestamp,
        "line_count": len(result_lines),
        "lines": result_lines,
    }


def record_stock_out(
    lines: Any,
    reason: Any,
    notes: Any = "",
    user_id: Any = None,
    db_path: Any = None,
    actor: Any = None,
) -> dict[str, Any]:
    """Remove stock: validate EVERY line, then write everything atomically.

    The sufficiency check runs after BEGIN IMMEDIATE, so no other process can
    change a balance between the read and the update. If any line is short,
    nothing at all is written.
    """
    permissions.require(actor, permissions.CAP_MANAGE_STOCK)
    clean_lines = _validate_lines(lines, with_expiry=False)
    clean_reason = validate_reason(reason)
    header_notes = validate_notes(notes)
    timestamp = database.local_timestamp()

    with database.transaction(_resolve_path(db_path)) as conn:
        ingredients = [
            _ingredient_for_movement(conn, line["ingredient_id"])
            for line in clean_lines
        ]
        # Check ALL lines before the first INSERT: a short line must not leave
        # a partly written transaction behind.
        for line, ingredient in zip(clean_lines, ingredients):
            available = ingredient["quantity_on_hand"]
            if line["quantity"] > available + 1e-9:
                raise ValueError(
                    f"Not enough {ingredient['name']}: "
                    f"{format_quantity(available, ingredient['unit'])} "
                    "on hand, tried to remove "
                    f"{format_quantity(line['quantity'], ingredient['unit'])}."
                )

        created_by = _resolve_user_id(conn, user_id)
        cursor = conn.execute(
            """
            INSERT INTO stock_out_transactions
                (transaction_date, reason, notes, created_by)
            VALUES (?, ?, ?, ?)
            """,
            (timestamp, clean_reason, header_notes or None, created_by),
        )
        transaction_id = int(cursor.lastrowid)

        result_lines: list[dict[str, Any]] = []
        for line, ingredient in zip(clean_lines, ingredients):
            conn.execute(
                """
                INSERT INTO stock_out_details
                    (stock_out_id, ingredient_id, quantity, unit, notes)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    transaction_id,
                    line["ingredient_id"],
                    line["quantity"],
                    ingredient["unit"],
                    line["notes"] or None,
                ),
            )
            new_balance = _balance_after(
                ingredient["quantity_on_hand"], -line["quantity"]
            )
            conn.execute(
                """
                UPDATE ingredients
                SET quantity_on_hand = ?, updated_at = ?
                WHERE ingredient_id = ?
                """,
                (new_balance, timestamp, line["ingredient_id"]),
            )
            result_lines.append(
                {
                    "name": ingredient["name"],
                    "quantity": line["quantity"],
                    "unit": ingredient["unit"],
                    "new_balance": new_balance,
                }
            )

    return {
        "movement_type": "OUT",
        "transaction_id": transaction_id,
        "transaction_date": timestamp,
        "line_count": len(result_lines),
        "lines": result_lines,
    }
