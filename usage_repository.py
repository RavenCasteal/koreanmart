"""
Stage 6  - ingredient usage calculator (READ-ONLY data layer).
Stage 11b - one dish at a time, chosen from the active menu.

This module answers one question: "Given ONE dish's recipe and the stock
currently on hand, could we make N servings, and what is short?"

Design rules:
  * It ONLY issues SELECT statements. There is no INSERT / UPDATE / DELETE
    anywhere in this file, and the connection is rolled back (never committed)
    when the read finishes, so even an accidental write could not be saved.
    Calculating NEVER deducts stock. To actually consume ingredients, the
    user records a stock-out on the Inventory screen (Stage 5).
  * Recipe amounts come from recipe_ingredients.quantity_per_serving.
    Physical balances come from ingredients.quantity_on_hand. Both are read
    inside one deferred read transaction so the result is a consistent
    snapshot of the database.
  * Servings must be a whole positive number. No fractions, no negatives.
  * "max_servings" is the largest number of COMPLETE servings the current
    stock can produce: min over all recipe lines of
    floor(quantity_on_hand / quantity_per_serving). A soft-deleted ingredient
    still referenced by the recipe makes that number 0.
  * Stage 11b: every query is pinned to EXACTLY ONE dish by menu_item_id.
    That is essential - otherwise the calculator would mix ingredient lines
    from different dishes. Omitting the id falls back to the primary POS
    dish (menu_items.singleton = 1), keeping Stage 1-11a callers working.

All SQL lives here; pages/calculator.py only formats the returned dict.
"""

from __future__ import annotations

import math
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator

import database

MAX_SERVINGS = 10_000
EPSILON = 1e-9

STATUS_ENOUGH = "Enough"
STATUS_SHORT = "Short"
STATUS_UNAVAILABLE = "Unavailable"


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _resolve_path(db_path: Any) -> Any:
    """Use the application database unless a test supplies a temp path."""
    return database.DB_PATH if db_path is None else db_path


@contextmanager
def _read_snapshot(db_path: Any) -> Iterator[sqlite3.Connection]:
    """One consistent, read-only view of the database.

    BEGIN (deferred) holds a read snapshot; rollback (never commit) at the
    end guarantees this module cannot persist any change.
    """
    conn = database.get_connection(db_path)
    try:
        conn.execute("BEGIN")
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    """Return plain dicts regardless of the connection's row_factory."""
    columns = [description[0] for description in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def validate_servings(value: Any) -> int:
    """Validate and normalise the number of servings from the form."""
    text = str(value if value is not None else "").strip()
    if not text:
        raise ValueError("Number of servings is required.")
    try:
        number = float(text)
    except ValueError as exc:
        raise ValueError(
            "Servings must be a whole number, e.g. 5 or 20."
        ) from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError("Servings must be greater than zero.")
    if number != int(number):
        raise ValueError("Servings must be a whole number, not a fraction.")
    if number > MAX_SERVINGS:
        raise ValueError(f"Servings must be {MAX_SERVINGS} or fewer.")
    return int(number)


# ----------------------------------------------------------------------
# Reads
# ----------------------------------------------------------------------
def get_calculator_context(db_path: Any = None) -> dict[str, Any]:
    """The PRIMARY POS dish's recipe header, its lines and current balances.

    Stage 11a pins the calculator to `singleton = 1`. Stage 11b's per-dish
    calculator was never finished and has been superseded by the Stage 12
    product model, so this legacy screen keeps its original unambiguous dish.
    """
    with _read_snapshot(_resolve_path(db_path)) as conn:
        recipe = _rows(
            conn.execute(
                """
                SELECT r.recipe_id,
                       r.notes,
                       r.updated_at,
                       m.menu_item_id,
                       m.name AS menu_item_name
                FROM recipes AS r
                JOIN menu_items AS m ON m.menu_item_id = r.menu_item_id
                WHERE m.is_active = 1 AND m.singleton = 1
                ORDER BY r.recipe_id
                LIMIT 1
                """
            )
        )
        lines = _rows(
            conn.execute(
                """
                SELECT ri.recipe_ingredient_id,
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
                JOIN recipes AS r
                  ON r.recipe_id = ri.recipe_id
                JOIN menu_items AS m
                  ON m.menu_item_id = r.menu_item_id
                WHERE m.is_active = 1 AND m.singleton = 1
                ORDER BY i.name COLLATE NOCASE
                """
            )
        )
    if not recipe:
        raise ValueError("No active recipe was found in the database.")
    return {"recipe": recipe[0], "lines": lines, "dishes": []}


def _line_result(line: dict[str, Any], servings: int) -> dict[str, Any]:
    """Compute one ingredient's requirement, shortfall and status."""
    per_serving = float(line["quantity_per_serving"])
    on_hand = float(line["quantity_on_hand"])
    is_active = bool(line["is_active"])

    required = per_serving * servings
    difference = on_hand - required
    # Tiny floating-point noise must not turn an exact match into a shortage.
    shortfall = max(0.0, -difference) if difference < -EPSILON else 0.0
    remaining = 0.0 if abs(difference) <= EPSILON else difference

    max_servings = (
        max(0, math.floor((on_hand + EPSILON) / per_serving))
        if per_serving > 0 else 0
    )
    if not is_active:
        status = STATUS_UNAVAILABLE
        max_servings = 0
    elif shortfall > EPSILON:
        status = STATUS_SHORT
    else:
        status = STATUS_ENOUGH

    return {
        "recipe_ingredient_id": int(line["recipe_ingredient_id"]),
        "ingredient_id": int(line["ingredient_id"]),
        "name": str(line["name"]),
        "unit": str(line["unit"]),
        "quantity_per_serving": per_serving,
        "quantity_required": required,
        "quantity_on_hand": on_hand,
        "shortfall": shortfall,
        "remaining_after": remaining,
        "max_servings": max_servings,
        "is_active": is_active,
        "status": status,
    }


def calculate(servings: Any, db_path: Any = None) -> dict[str, Any]:
    """What-if calculation for N servings of ONE dish. Pure read.

    Returns the per-ingredient rows plus an overall verdict:
      sufficient             - True when every line has enough stock
      max_servings           - complete servings possible with current stock
      limiting_ingredients   - names that cap max_servings
      shortages              - lines that are short or unavailable
    """
    count = validate_servings(servings)
    context = get_calculator_context(db_path)
    lines = context["lines"]
    dish_name = str(context["recipe"]["menu_item_name"])
    if not lines:
        raise ValueError(
            f"The {dish_name} recipe has no ingredients yet. "
            "Add some on the Recipe screen before calculating."
        )

    rows = [_line_result(line, count) for line in lines]
    shortages = [row for row in rows if row["status"] != STATUS_ENOUGH]
    max_servings = min(row["max_servings"] for row in rows)
    limiting = [
        row["name"] for row in rows if row["max_servings"] == max_servings
    ]

    return {
        "servings": count,
        "recipe_id": int(context["recipe"]["recipe_id"]),
        "menu_item_id": int(context["recipe"]["menu_item_id"]),
        "menu_item_name": dish_name,
        "dishes": context["dishes"],
        "recipe_updated_at": context["recipe"]["updated_at"],
        "ingredient_count": len(rows),
        "rows": rows,
        "sufficient": not shortages,
        "shortage_count": len(shortages),
        "shortages": shortages,
        "max_servings": max_servings,
        "limiting_ingredients": limiting,
    }


def get_max_servings(db_path: Any = None) -> int:
    """How many complete servings of the primary dish can be made right now."""
    return int(calculate(1, db_path=db_path)["max_servings"])
