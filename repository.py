"""
Stage 3 - ingredient data access layer.

All ingredient reads/writes live here so the user-interface files never
contain SQL. Everything uses database.transaction(), which opens a connection
with foreign keys enabled and commits/rolls back automatically.

Important rules enforced here:
  * quantity_on_hand is NEVER written by this module. Only stock-in / stock-out
    transactions in stock_repository.py may change a physical balance.
  * Deleting is a SOFT delete (is_active = 0) so transaction history keeps a
    valid foreign key. Recipe lines pointing at the removed ingredient are
    deleted in the same transaction, with the count reported to the caller.
  * Duplicate names are rejected among ACTIVE ingredients only.
"""

from __future__ import annotations

import math
import re
import sqlite3
from datetime import datetime
from typing import Any

import database
import permissions
# Stage 8: one canonical rule for the whole app. Re-exported here so all
# existing callers (and monitoring_repository.py) keep the same names.
from stock_rules import EXPIRY_WARNING_DAYS, expiry_status, stock_status

# Suggested units shown in the form; the field stays freely editable.
DEFAULT_UNITS: tuple[str, ...] = ("grams", "ml", "piece", "sheet")

_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------
def _resolve_path(db_path: Any) -> Any:
    """Use the application database unless a test supplies a temp path."""
    return database.DB_PATH if db_path is None else db_path


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    """Return plain dicts regardless of the connection's row_factory."""
    columns = [description[0] for description in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _clean_name(value: Any) -> str:
    name = str(value or "").strip()
    if not name:
        raise ValueError("Ingredient name is required.")
    if len(name) > 60:
        raise ValueError("Ingredient name must be 60 characters or fewer.")
    return name


def _clean_unit(value: Any) -> str:
    unit = str(value or "").strip()
    if not unit:
        raise ValueError("Measurement unit is required.")
    if len(unit) > 20:
        raise ValueError("Measurement unit must be 20 characters or fewer.")
    return unit


def _clean_minimum(value: Any) -> float:
    text = str(value if value is not None else "").strip()
    if not text:
        raise ValueError("Minimum stock is required (use 0 if none).")
    try:
        number = float(text)
    except ValueError as exc:
        raise ValueError("Minimum stock must be a number, e.g. 150 or 2.5.") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError("Minimum stock must be zero or a positive number.")
    return number


def _clean_expiry(value: Any) -> str | None:
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


def validate(
    name: Any, unit: Any, minimum_stock: Any, expiration_date: Any
) -> dict[str, Any]:
    """Validate and normalise one ingredient form submission."""
    return {
        "name": _clean_name(name),
        "unit": _clean_unit(unit),
        "minimum_stock": _clean_minimum(minimum_stock),
        "expiration_date": _clean_expiry(expiration_date),
    }


# stock_status() and expiry_status() are re-exported from stock_rules.py
# above. All pages and monitoring_repository.py keep using repository.X.


# ----------------------------------------------------------------------
# Reads
# ----------------------------------------------------------------------
def list_ingredients(
    search: str = "", db_path: Any = None, include_inactive: bool = False
) -> list[dict[str, Any]]:
    """Active ingredients, optionally filtered by name or unit (SQL LIKE)."""
    path = _resolve_path(db_path)
    conditions = ["is_active = 1"] if not include_inactive else []
    params: list[Any] = []

    keyword = str(search or "").strip()
    if keyword:
        # Escape LIKE wildcards so a literal '%' in a search is harmless.
        escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conditions.append(
            "(name LIKE ? ESCAPE '\\' OR unit LIKE ? ESCAPE '\\')"
        )
        params.extend([f"%{escaped}%", f"%{escaped}%"])

    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    sql = f"""
        SELECT ingredient_id, name, unit, quantity_on_hand, minimum_stock,
               expiration_date, is_active, created_at, updated_at
        FROM ingredients
        {where}
        ORDER BY name COLLATE NOCASE
    """
    with database.transaction(path) as conn:
        cursor = conn.execute(sql, params)
        rows = _rows(cursor)

    for row in rows:
        row["status"] = stock_status(row["quantity_on_hand"], row["minimum_stock"])
        row["expiry_status"] = expiry_status(row["expiration_date"])
    return rows


def get_ingredient(ingredient_id: int, db_path: Any = None) -> dict[str, Any] | None:
    """Fetch one ingredient, or None if it does not exist / was removed."""
    with database.transaction(_resolve_path(db_path)) as conn:
        cursor = conn.execute(
            """
            SELECT ingredient_id, name, unit, quantity_on_hand, minimum_stock,
                   expiration_date, is_active, created_at, updated_at
            FROM ingredients
            WHERE ingredient_id = ?
            """,
            (ingredient_id,),
        )
        rows = _rows(cursor)
    return rows[0] if rows else None


def get_unit_choices(db_path: Any = None) -> list[str]:
    """Suggested units: the defaults plus any unit already stored in SQLite."""
    with database.transaction(_resolve_path(db_path)) as conn:
        cursor = conn.execute(
            "SELECT DISTINCT unit FROM ingredients WHERE is_active = 1 ORDER BY unit"
        )
        stored = [row[0] for row in cursor.fetchall()]
    merged: list[str] = []
    for unit in (*DEFAULT_UNITS, *stored):
        if unit not in merged:
            merged.append(unit)
    return merged


def count_recipe_links(ingredient_id: int, db_path: Any = None) -> int:
    """How many recipe lines would be removed by deleting this ingredient."""
    with database.transaction(_resolve_path(db_path)) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM recipe_ingredients WHERE ingredient_id = ?",
            (ingredient_id,),
        ).fetchone()[0]


def count_active(db_path: Any = None) -> int:
    with database.transaction(_resolve_path(db_path)) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM ingredients WHERE is_active = 1"
        ).fetchone()[0]


# ----------------------------------------------------------------------
# Writes
# ----------------------------------------------------------------------
def _assert_name_available(
    conn: sqlite3.Connection, name: str, excluded_id: int | None
) -> None:
    cursor = conn.execute(
        """
        SELECT ingredient_id FROM ingredients
        WHERE is_active = 1 AND name = ? COLLATE NOCASE AND ingredient_id <> ?
        """,
        (name, excluded_id if excluded_id is not None else -1),
    )
    if cursor.fetchone() is not None:
        raise ValueError(f"An active ingredient named '{name}' already exists.")


def add_ingredient(
    name: Any,
    unit: Any,
    minimum_stock: Any,
    expiration_date: Any,
    db_path: Any = None,
    actor: Any = None,
) -> int:
    """Create an ingredient with ZERO stock. Returns the new ingredient_id."""
    permissions.require(actor, permissions.CAP_MANAGE_INGREDIENTS)
    clean = validate(name, unit, minimum_stock, expiration_date)
    timestamp = _now()
    with database.transaction(_resolve_path(db_path)) as conn:
        _assert_name_available(conn, clean["name"], None)
        try:
            cursor = conn.execute(
                """
                INSERT INTO ingredients
                    (name, unit, quantity_on_hand, minimum_stock,
                     expiration_date, is_active, created_at, updated_at)
                VALUES (?, ?, 0, ?, ?, 1, ?, ?)
                """,
                (
                    clean["name"],
                    clean["unit"],
                    clean["minimum_stock"],
                    clean["expiration_date"],
                    timestamp,
                    timestamp,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"Could not add ingredient: {exc}") from exc
        return int(cursor.lastrowid)


def update_ingredient(
    ingredient_id: int,
    name: Any,
    unit: Any,
    minimum_stock: Any,
    expiration_date: Any,
    db_path: Any = None,
    actor: Any = None,
) -> None:
    """Edit catalog fields only. quantity_on_hand is deliberately untouched."""
    permissions.require(actor, permissions.CAP_MANAGE_INGREDIENTS)
    clean = validate(name, unit, minimum_stock, expiration_date)
    with database.transaction(_resolve_path(db_path)) as conn:
        exists = conn.execute(
            "SELECT 1 FROM ingredients WHERE ingredient_id = ? AND is_active = 1",
            (ingredient_id,),
        ).fetchone()
        if exists is None:
            raise ValueError("That ingredient no longer exists.")
        _assert_name_available(conn, clean["name"], ingredient_id)
        try:
            conn.execute(
                """
                UPDATE ingredients
                SET name = ?, unit = ?, minimum_stock = ?, expiration_date = ?,
                    updated_at = ?
                WHERE ingredient_id = ? AND is_active = 1
                """,
                (
                    clean["name"],
                    clean["unit"],
                    clean["minimum_stock"],
                    clean["expiration_date"],
                    _now(),
                    ingredient_id,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"Could not update ingredient: {exc}") from exc


def delete_ingredient(
    ingredient_id: int, db_path: Any = None, actor: Any = None
) -> dict[str, Any]:
    """Soft-delete an ingredient and remove its recipe lines atomically.

    The physical ingredient row is kept so stock-in / stock-out history keeps
    a valid foreign key. Returns a summary for the confirmation message.
    """
    permissions.require(actor, permissions.CAP_MANAGE_INGREDIENTS)
    with database.transaction(_resolve_path(db_path)) as conn:
        cursor = conn.execute(
            """
            SELECT name, quantity_on_hand FROM ingredients
            WHERE ingredient_id = ? AND is_active = 1
            """,
            (ingredient_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise ValueError("That ingredient no longer exists.")
        name = row[0]
        balance = row[1]

        links = conn.execute(
            "SELECT COUNT(*) FROM recipe_ingredients WHERE ingredient_id = ?",
            (ingredient_id,),
        ).fetchone()[0]

        conn.execute(
            "DELETE FROM recipe_ingredients WHERE ingredient_id = ?",
            (ingredient_id,),
        )
        conn.execute(
            "UPDATE ingredients SET is_active = 0, updated_at = ? "
            "WHERE ingredient_id = ?",
            (_now(), ingredient_id),
        )

    return {
        "name": name,
        "quantity_on_hand": balance,
        "recipe_links_removed": links,
    }
