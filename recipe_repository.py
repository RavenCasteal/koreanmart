"""
Stage 4 - recipe (bill of materials) data access layer.
Stage 11a - recipes are now PER DISH.

Every dish in menu_items has its own recipes row and its own set of
recipe_ingredients lines. Calling a function WITHOUT menu_item_id uses the
PRIMARY POS dish (singleton = 1), which keeps every Stage 1-10 call and test
working unchanged. The Recipe screen passes the dish the manager selected.

Rules enforced by this module:
  * A write always resolves the recipe from the database, never from a raw
    recipe id supplied by the interface, so one dish cannot edit another's
    recipe by accident.
  * Recipe amounts are stored in recipe_ingredients.quantity_per_serving.
    Physical stock lives in ingredients.quantity_on_hand. This module NEVER
    writes a stock balance and never creates a stock-in / stock-out record.
  * Amounts must be > 0 and finite; an ingredient may appear in one dish's
    recipe only once; a line may only reference an ACTIVE ingredient.
  * A recipe must keep at least one ingredient line. A brand-new dish starts
    with an empty recipe, which is fine - it simply cannot be calculated or
    sold until ingredients are added.

All SQL lives here; pages/recipe.py only builds the interface.
"""

from __future__ import annotations

import math
import sqlite3
from typing import Any

import database
import permissions
import repository

MAX_NOTES_LENGTH = 200
MAX_QUANTITY = 1_000_000.0


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


def validate_quantity(value: Any) -> float:
    """Validate and normalise one 'quantity per serving' form value."""
    text = str(value if value is not None else "").strip()
    if not text:
        raise ValueError("Quantity per serving is required.")
    try:
        number = float(text)
    except ValueError as exc:
        raise ValueError(
            "Quantity per serving must be a number, e.g. 150 or 1.5."
        ) from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError("Quantity per serving must be greater than zero.")
    if number > MAX_QUANTITY:
        raise ValueError(
            f"Quantity per serving must be {MAX_QUANTITY:g} or less."
        )
    return number


def validate_notes(value: Any) -> str:
    """Recipe notes are optional free text with a sensible length limit."""
    notes = str(value if value is not None else "").strip()
    if len(notes) > MAX_NOTES_LENGTH:
        raise ValueError(
            f"Recipe notes must be {MAX_NOTES_LENGTH} characters or fewer."
        )
    return notes


def _resolve_recipe_id(
    conn: sqlite3.Connection, menu_item_id: Any = None
) -> int:
    """Recipe id for the chosen dish, or for the primary POS dish by default.

    Stage 11a: several dishes may be active, so "the first active recipe" is
    no longer unambiguous. Pinning the default to singleton = 1 is what keeps
    the Calculator and POS behaviour identical to Stage 10.
    """
    if menu_item_id is None:
        row = conn.execute(
            """
            SELECT r.recipe_id
            FROM recipes AS r
            JOIN menu_items AS m ON m.menu_item_id = r.menu_item_id
            WHERE m.singleton = 1 AND m.is_active = 1
            ORDER BY r.recipe_id
            LIMIT 1
            """
        ).fetchone()
    else:
        try:
            key = int(menu_item_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("Please choose a dish.") from exc
        row = conn.execute(
            """
            SELECT r.recipe_id
            FROM recipes AS r
            JOIN menu_items AS m ON m.menu_item_id = r.menu_item_id
            WHERE r.menu_item_id = ? AND m.is_active = 1
            """,
            (key,),
        ).fetchone()
    if row is None:
        raise ValueError("No active recipe was found for that dish.")
    return int(row[0])


# Backward-compatible name used by Stage 4-10 callers/tests.
def _active_recipe_id(conn: sqlite3.Connection) -> int:
    return _resolve_recipe_id(conn)


def _touch_recipe(conn: sqlite3.Connection, recipe_id: int) -> None:
    """Record when the recipe definition was last changed."""
    conn.execute(
        "UPDATE recipes SET updated_at = ? WHERE recipe_id = ?",
        (database.local_timestamp(), recipe_id),
    )


# ----------------------------------------------------------------------
# Reads
# ----------------------------------------------------------------------
def get_recipe(
    db_path: Any = None, menu_item_id: Any = None
) -> dict[str, Any]:
    """Return one dish's recipe header plus live counts from SQLite."""
    with database.transaction(_resolve_path(db_path)) as conn:
        recipe_id = _resolve_recipe_id(conn, menu_item_id)
        rows = _rows(
            conn.execute(
                """
                SELECT r.recipe_id,
                       r.menu_item_id,
                       r.notes,
                       r.updated_at,
                       m.name AS menu_item_name,
                       m.unit_price,
                       m.singleton,
                       (SELECT COUNT(*) FROM recipe_ingredients AS ri
                        WHERE ri.recipe_id = r.recipe_id) AS ingredient_count,
                       (SELECT COUNT(DISTINCT i.unit)
                        FROM recipe_ingredients AS ri
                        JOIN ingredients AS i
                          ON i.ingredient_id = ri.ingredient_id
                        WHERE ri.recipe_id = r.recipe_id) AS unit_count
                FROM recipes AS r
                JOIN menu_items AS m ON m.menu_item_id = r.menu_item_id
                WHERE r.recipe_id = ?
                """,
                (recipe_id,),
            )
        )
    if not rows:
        raise ValueError("No active recipe was found for that dish.")
    return rows[0]


def list_recipe_lines(
    db_path: Any = None, menu_item_id: Any = None
) -> list[dict[str, Any]]:
    """Every ingredient needed for ONE serving of the chosen dish.

    quantity_on_hand is included for display only: it is read-only context,
    never a value this page may change. The recipe_id filter is essential in
    Stage 11a - without it, lines from all dishes would be mixed together.
    """
    with database.transaction(_resolve_path(db_path)) as conn:
        recipe_id = _resolve_recipe_id(conn, menu_item_id)
        rows = _rows(
            conn.execute(
                """
                SELECT ri.recipe_ingredient_id,
                       ri.recipe_id,
                       ri.ingredient_id,
                       ri.quantity_per_serving,
                       i.name,
                       i.unit,
                       i.quantity_on_hand,
                       i.minimum_stock,
                       i.is_active
                FROM recipe_ingredients AS ri
                JOIN ingredients AS i
                  ON i.ingredient_id = ri.ingredient_id
                WHERE ri.recipe_id = ?
                ORDER BY i.name COLLATE NOCASE
                """,
                (recipe_id,),
            )
        )
    for row in rows:
        row["status"] = repository.stock_status(
            row["quantity_on_hand"], row["minimum_stock"]
        )
    return rows


def list_available_ingredients(
    db_path: Any = None, menu_item_id: Any = None
) -> list[dict[str, Any]]:
    """Active catalog ingredients NOT yet used in THIS dish's recipe.

    Used by the 'Add ingredient to recipe' dialog. Stage 3 soft-deleted
    ingredients are excluded so a removed ingredient cannot be re-added.
    An ingredient used by another dish is still available here - dishes
    legitimately share the same raw materials.
    """
    with database.transaction(_resolve_path(db_path)) as conn:
        recipe_id = _resolve_recipe_id(conn, menu_item_id)
        return _rows(
            conn.execute(
                """
                SELECT i.ingredient_id, i.name, i.unit, i.quantity_on_hand
                FROM ingredients AS i
                WHERE i.is_active = 1
                  AND NOT EXISTS (
                      SELECT 1 FROM recipe_ingredients AS ri
                      WHERE ri.ingredient_id = i.ingredient_id
                        AND ri.recipe_id = ?
                  )
                ORDER BY i.name COLLATE NOCASE
                """,
                (recipe_id,),
            )
        )


# ----------------------------------------------------------------------
# Writes (all in one transaction each)
# ----------------------------------------------------------------------
def update_quantity(
    recipe_ingredient_id: int,
    value: Any,
    db_path: Any = None,
    actor: Any = None,
    menu_item_id: Any = None,
) -> dict[str, Any]:
    """Change how much of an ingredient ONE serving of this dish requires."""
    permissions.require(actor, permissions.CAP_MANAGE_RECIPE)
    quantity = validate_quantity(value)
    with database.transaction(_resolve_path(db_path)) as conn:
        recipe_id = _resolve_recipe_id(conn, menu_item_id)
        row = conn.execute(
            """
            SELECT ri.recipe_id, i.name
            FROM recipe_ingredients AS ri
            JOIN ingredients AS i ON i.ingredient_id = ri.ingredient_id
            WHERE ri.recipe_ingredient_id = ?
            """,
            (recipe_ingredient_id,),
        ).fetchone()
        if row is None or int(row[0]) != recipe_id:
            raise ValueError("That recipe line no longer exists.")
        name = str(row[1])
        conn.execute(
            """
            UPDATE recipe_ingredients
            SET quantity_per_serving = ?
            WHERE recipe_ingredient_id = ?
            """,
            (quantity, recipe_ingredient_id),
        )
        _touch_recipe(conn, recipe_id)
    return {"name": name, "quantity_per_serving": quantity}


def add_ingredient(
    ingredient_id: int,
    quantity: Any,
    db_path: Any = None,
    actor: Any = None,
    menu_item_id: Any = None,
) -> dict[str, Any]:
    """Add one catalog ingredient to this dish's recipe."""
    permissions.require(actor, permissions.CAP_MANAGE_RECIPE)
    amount = validate_quantity(quantity)
    try:
        ingredient_id = int(ingredient_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("Please choose an ingredient.") from exc

    with database.transaction(_resolve_path(db_path)) as conn:
        recipe_id = _resolve_recipe_id(conn, menu_item_id)
        ingredient = conn.execute(
            """
            SELECT name, unit, is_active FROM ingredients
            WHERE ingredient_id = ?
            """,
            (ingredient_id,),
        ).fetchone()
        if ingredient is None:
            raise ValueError("That ingredient no longer exists.")
        name, unit, active = str(ingredient[0]), str(ingredient[1]), ingredient[2]
        if active != 1:
            raise ValueError("Only active ingredients can be added to a recipe.")
        duplicate = conn.execute(
            """
            SELECT 1 FROM recipe_ingredients
            WHERE recipe_id = ? AND ingredient_id = ?
            """,
            (recipe_id, ingredient_id),
        ).fetchone()
        if duplicate is not None:
            raise ValueError(f"'{name}' is already in the recipe.")

        try:
            cursor = conn.execute(
                """
                INSERT INTO recipe_ingredients
                    (recipe_id, ingredient_id, quantity_per_serving)
                VALUES (?, ?, ?)
                """,
                (recipe_id, ingredient_id, amount),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"Could not add recipe line: {exc}") from exc
        _touch_recipe(conn, recipe_id)
        line_id = int(cursor.lastrowid)

    return {
        "recipe_ingredient_id": line_id,
        "name": name,
        "unit": unit,
        "quantity_per_serving": amount,
    }


def remove_ingredient(
    recipe_ingredient_id: int,
    db_path: Any = None,
    actor: Any = None,
    menu_item_id: Any = None,
) -> dict[str, Any]:
    """Remove one recipe line (the ingredient stays in the catalog)."""
    permissions.require(actor, permissions.CAP_MANAGE_RECIPE)
    with database.transaction(_resolve_path(db_path)) as conn:
        recipe_id = _resolve_recipe_id(conn, menu_item_id)
        row = conn.execute(
            """
            SELECT ri.recipe_id, i.name, ri.quantity_per_serving, i.unit
            FROM recipe_ingredients AS ri
            JOIN ingredients AS i ON i.ingredient_id = ri.ingredient_id
            WHERE ri.recipe_ingredient_id = ?
            """,
            (recipe_ingredient_id,),
        ).fetchone()
        if row is None or int(row[0]) != recipe_id:
            raise ValueError("That recipe line no longer exists.")
        name, amount, unit = row[1], row[2], row[3]

        remaining = conn.execute(
            "SELECT COUNT(*) FROM recipe_ingredients WHERE recipe_id = ?",
            (recipe_id,),
        ).fetchone()[0]
        if remaining <= 1:
            raise ValueError(
                "A recipe must keep at least one ingredient. "
                "Add another one before removing this."
            )

        conn.execute(
            "DELETE FROM recipe_ingredients WHERE recipe_ingredient_id = ?",
            (recipe_ingredient_id,),
        )
        _touch_recipe(conn, recipe_id)

    return {"name": name, "quantity_per_serving": amount, "unit": unit}


def update_notes(
    notes: Any,
    db_path: Any = None,
    actor: Any = None,
    menu_item_id: Any = None,
) -> str:
    """Save this dish's free-text recipe notes (e.g. prep reminders)."""
    permissions.require(actor, permissions.CAP_MANAGE_RECIPE)
    clean = validate_notes(notes)
    with database.transaction(_resolve_path(db_path)) as conn:
        recipe_id = _resolve_recipe_id(conn, menu_item_id)
        conn.execute(
            """
            UPDATE recipes SET notes = ?, updated_at = ?
            WHERE recipe_id = ?
            """,
            (clean, database.local_timestamp(), recipe_id),
        )
    return clean
