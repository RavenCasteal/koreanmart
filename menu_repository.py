"""
Stage 11a - menu (dish) data access layer.

The original capstone allowed exactly ONE sellable dish ("Regular Ramen").
Stage 11a lets a manager add more dishes. Each dish has:

    menu_items row  -> name, description, unit_price (PHP), active flag
    recipes row     -> its OWN bill of materials (one recipe per dish)
    recipe_ingredients -> per-serving amounts for THAT dish only

Physical stock is NOT per dish: ingredients.quantity_on_hand stays shared.
That is intentional - both ramen variants use the same noodles in the same
storeroom.

Rules enforced here:
  * Manager only. Reads are guarded too, so a cashier screen cannot enumerate
    dishes or prices for MANAGEMENT. (POS has its own harmless read-only list
    of active dish names so the cashier can take an order.)
  * Exactly ONE "primary" dish (singleton = 1) exists. Since Stage 11b it is
    simply the dish the POS screen selects by default; EVERY active dish is
    sellable. New dishes are always singleton = 0.
  * A dish is deactivated, never deleted. Its id, recipe and every past sale
    snapshot stay intact.
  * Any dish may be renamed or deactivated now that receipts and sales rows
    store name/price snapshots. Only the LAST active dish cannot be turned
    off, because the POS screen needs something to sell.
  * Prices are Philippine peso (PHP), always > 0 and rounded to 2 decimals.

All SQL lives here; pages/menu.py only builds the interface.
"""

from __future__ import annotations

import math
import sqlite3
from typing import Any

import database
import permissions

MAX_NAME_LENGTH = 60
MAX_DESCRIPTION_LENGTH = 200
MAX_PRICE = 1_000_000.0

PRIMARY_LABEL = "Default POS dish"


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


def validate_name(value: Any) -> str:
    """A short, non-empty, unique-per-database dish name."""
    name = str(value if value is not None else "").strip()
    if not name:
        raise ValueError("Dish name is required.")
    if len(name) > MAX_NAME_LENGTH:
        raise ValueError(
            f"Dish name must be {MAX_NAME_LENGTH} characters or fewer."
        )
    return name


def validate_description(value: Any) -> str:
    """Optional free text shown on the Menu screen."""
    text = str(value if value is not None else "").strip()
    if len(text) > MAX_DESCRIPTION_LENGTH:
        raise ValueError(
            f"Description must be {MAX_DESCRIPTION_LENGTH} characters or fewer."
        )
    return text


def validate_unit_price(value: Any) -> float:
    """Positive PHP price, rounded to cents (same rule as the POS screen)."""
    text = str(value if value is not None else "").strip()
    if not text:
        raise ValueError("Unit price is required.")
    cleaned = text.replace("\u20b1", "").replace(",", "").strip()
    try:
        number = float(cleaned)
    except ValueError as exc:
        raise ValueError("Unit price must be a number, e.g. 150 or 150.00.") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError("Unit price must be greater than zero.")
    if number > MAX_PRICE:
        raise ValueError(f"Unit price must be {MAX_PRICE:g} or less.")
    return round(number, 2)


def _dish_row(conn: sqlite3.Connection, menu_item_id: Any) -> sqlite3.Row:
    try:
        key = int(menu_item_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("Please choose a dish.") from exc
    row = conn.execute(
        "SELECT * FROM menu_items WHERE menu_item_id = ?", (key,)
    ).fetchone()
    if row is None:
        raise ValueError("That dish no longer exists.")
    return row


def primary_dish_id(conn: sqlite3.Connection) -> int | None:
    """Id of the one dish the unchanged POS screen sells, if any."""
    row = conn.execute(
        "SELECT menu_item_id FROM menu_items WHERE singleton = 1"
    ).fetchone()
    return int(row[0]) if row else None


# ----------------------------------------------------------------------
# Reads (manager only)
# ----------------------------------------------------------------------
def list_dishes(
    db_path: Any = None,
    actor: Any = None,
    include_inactive: bool = True,
) -> list[dict[str, Any]]:
    """Every dish with its recipe id and ingredient-line count."""
    permissions.require(actor, permissions.CAP_MANAGE_MENU)
    where = "" if include_inactive else "WHERE m.is_active = 1"
    with database.transaction(_resolve_path(db_path)) as conn:
        return _rows(
            conn.execute(
                f"""
                SELECT m.menu_item_id,
                       m.name,
                       m.description,
                       m.unit_price,
                       m.is_active,
                       m.singleton,
                       m.created_at,
                       r.recipe_id,
                       (SELECT COUNT(*) FROM recipe_ingredients AS ri
                        WHERE ri.recipe_id = r.recipe_id) AS ingredient_count
                FROM menu_items AS m
                LEFT JOIN recipes AS r ON r.menu_item_id = m.menu_item_id
                {where}
                ORDER BY m.singleton DESC, m.name COLLATE NOCASE
                """
            )
        )


def get_dish(
    menu_item_id: Any, db_path: Any = None, actor: Any = None
) -> dict[str, Any]:
    """One dish, used by the Edit dialog."""
    permissions.require(actor, permissions.CAP_MANAGE_MENU)
    with database.transaction(_resolve_path(db_path)) as conn:
        return dict(_dish_row(conn, menu_item_id))


def summary(
    db_path: Any = None, actor: Any = None
) -> dict[str, Any]:
    """Small live counts for the Menu page summary cards."""
    permissions.require(actor, permissions.CAP_MANAGE_MENU)
    with database.transaction(_resolve_path(db_path)) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(is_active), 0) AS active,
                   COALESCE(SUM(CASE WHEN is_active = 0 THEN 1 ELSE 0 END), 0)
                       AS inactive,
                   COALESCE(SUM(CASE WHEN singleton = 1 THEN 1 ELSE 0 END), 0)
                       AS primary_count
            FROM menu_items
            """
        ).fetchone()
        with_lines = conn.execute(
            """
            SELECT COUNT(DISTINCT r.menu_item_id)
            FROM recipes AS r
            JOIN recipe_ingredients AS ri ON ri.recipe_id = r.recipe_id
            """
        ).fetchone()[0]
    return {
        "total": int(row["total"]),
        "active": int(row["active"]),
        "inactive": int(row["inactive"]),
        "primary_count": int(row["primary_count"]),
        "with_recipe": int(with_lines),
    }


# ----------------------------------------------------------------------
# Writes (all in one atomic transaction each)
# ----------------------------------------------------------------------
def add_dish(
    name: Any,
    description: Any,
    unit_price: Any,
    db_path: Any = None,
    actor: Any = None,
) -> int:
    """Add a new dish AND its own empty recipe. Returns the new dish id.

    New dishes are ordinary (singleton = 0); Stage 11b will let the cashier
    pick them on the POS screen. An empty recipe is created immediately so
    the Recipe screen can assign ingredients to it.
    """
    permissions.require(actor, permissions.CAP_MANAGE_MENU)
    clean_name = validate_name(name)
    clean_description = validate_description(description)
    price = validate_unit_price(unit_price)

    with database.transaction(_resolve_path(db_path)) as conn:
        duplicate = conn.execute(
            "SELECT 1 FROM menu_items WHERE name = ? COLLATE NOCASE",
            (clean_name,),
        ).fetchone()
        if duplicate is not None:
            raise ValueError(f"A dish named '{clean_name}' already exists.")
        try:
            cursor = conn.execute(
                """
                INSERT INTO menu_items
                    (name, description, is_active, singleton, unit_price,
                     created_at)
                VALUES (?, ?, 1, 0, ?, ?)
                """,
                (
                    clean_name,
                    clean_description,
                    price,
                    database.local_timestamp(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"Could not add the dish: {exc}") from exc
        dish_id = int(cursor.lastrowid)
        conn.execute(
            """
            INSERT INTO recipes (menu_item_id, notes, updated_at)
            VALUES (?, ?, ?)
            """,
            (
                dish_id,
                "New dish - add its ingredients on the Recipe screen.",
                database.local_timestamp(),
            ),
        )
    return dish_id


def update_dish(
    menu_item_id: Any,
    name: Any,
    description: Any,
    unit_price: Any,
    db_path: Any = None,
    actor: Any = None,
) -> dict[str, Any]:
    """Rename / reprice / redescribe one dish. Never touches stock or sales."""
    permissions.require(actor, permissions.CAP_MANAGE_MENU)
    clean_name = validate_name(name)
    clean_description = validate_description(description)
    price = validate_unit_price(unit_price)

    with database.transaction(_resolve_path(db_path)) as conn:
        row = _dish_row(conn, menu_item_id)
        # Stage 11a rule restored: the primary POS dish keeps its name, so
        # the legacy POS screen and archived receipts stay consistent. Its
        # price and description remain editable.
        if int(row["singleton"]) == 1 and clean_name != str(row["name"]):
            raise ValueError(
                "The primary POS dish keeps its name. Its price and "
                "description can still be edited."
            )
        duplicate = conn.execute(
            """
            SELECT 1 FROM menu_items
            WHERE name = ? COLLATE NOCASE AND menu_item_id <> ?
            """,
            (clean_name, int(row["menu_item_id"])),
        ).fetchone()
        if duplicate is not None:
            raise ValueError(f"A dish named '{clean_name}' already exists.")
        try:
            conn.execute(
                """
                UPDATE menu_items
                SET name = ?, description = ?, unit_price = ?
                WHERE menu_item_id = ?
                """,
                (
                    clean_name,
                    clean_description,
                    price,
                    int(row["menu_item_id"]),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"Could not update the dish: {exc}") from exc
    return {"menu_item_id": int(row["menu_item_id"]), "name": clean_name}


def set_active(
    menu_item_id: Any,
    active: Any,
    db_path: Any = None,
    actor: Any = None,
) -> dict[str, Any]:
    """Deactivate / reactivate a dish. A status change, NEVER a delete."""
    permissions.require(actor, permissions.CAP_MANAGE_MENU)
    flag = 1 if bool(active) else 0
    with database.transaction(_resolve_path(db_path)) as conn:
        row = _dish_row(conn, menu_item_id)
        if not flag and int(row["singleton"]) == 1:
            # Stage 11a rule restored: the legacy POS screen sells exactly
            # one primary dish, so it must never disappear.
            raise ValueError(
                "The primary POS dish cannot be deactivated: the legacy "
                "Point of Sale screen needs it."
            )
        conn.execute(
            "UPDATE menu_items SET is_active = ? WHERE menu_item_id = ?",
            (flag, int(row["menu_item_id"])),
        )
    return {
        "menu_item_id": int(row["menu_item_id"]),
        "name": str(row["name"]),
        "is_active": flag,
    }
