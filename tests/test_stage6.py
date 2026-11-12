"""
Stage 6 automated checks - ingredient usage calculator.

Run from the project folder:
    python -m unittest discover -s tests -v

Uses a temporary database; the real database/ramen_inventory.db is untouched.
No GUI is opened. The single most important guarantee here is that a
calculation NEVER deducts stock and NEVER writes anything to SQLite.
"""

from __future__ import annotations

import ast
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import database
import recipe_repository as rr
import repository
import stock_repository as stock
import usage_repository as usage


class Stage6CalculatorTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.db_path = Path(temp_dir.name) / "ramen_inventory.db"
        database.initialize_database(self.db_path)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _raw(self, sql: str, params: tuple = ()) -> list[tuple]:
        with database.transaction(self.db_path) as conn:
            return [tuple(row) for row in conn.execute(sql, params).fetchall()]

    def _calc(self, servings: Any) -> dict:
        return usage.calculate(servings, db_path=self.db_path)

    def _hash(self) -> str:
        return hashlib.sha256(self.db_path.read_bytes()).hexdigest()

    def _stock_in_all(self, quantity: Any) -> None:
        """Put the same amount of every recipe ingredient on hand."""
        lines = [
            {"ingredient_id": int(row["ingredient_id"]), "quantity": quantity}
            for row in rr.list_recipe_lines(db_path=self.db_path)
        ]
        stock.record_stock_in(lines, user_id=1, db_path=self.db_path)

    def _stock_in_for(self, servings: int) -> None:
        """Put EXACTLY enough stock on hand for N servings, no more."""
        lines = [
            {
                "ingredient_id": int(row["ingredient_id"]),
                "quantity": row["quantity_per_serving"] * servings,
            }
            for row in rr.list_recipe_lines(db_path=self.db_path)
        ]
        stock.record_stock_in(lines, user_id=1, db_path=self.db_path)

    def _balance(self, name: str) -> float:
        return float(
            self._raw(
                "SELECT quantity_on_hand FROM ingredients WHERE name = ?", (name,)
            )[0][0]
        )

    def _row(self, result: dict, name: str) -> dict:
        return next(row for row in result["rows"] if row["name"] == name)

    # ------------------------------------------------------------------
    # Servings validation
    # ------------------------------------------------------------------
    def test_validate_servings_accepts_integers_and_numeric_strings(self) -> None:
        self.assertEqual(usage.validate_servings(1), 1)
        self.assertEqual(usage.validate_servings("4"), 4)
        self.assertEqual(usage.validate_servings("  12  "), 12)
        self.assertEqual(usage.validate_servings(3.0), 3)
        self.assertEqual(usage.validate_servings("25"), 25)

    def test_validate_servings_rejects_empty_and_non_numeric(self) -> None:
        for value in ("", "   ", None, "abc", "5 servings"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                usage.validate_servings(value)

    def test_validate_servings_rejects_zero_negative_and_fractions(self) -> None:
        for value in ("0", "-1", "2.5", 0.5, -3):
            with self.subTest(value=value), self.assertRaises(ValueError):
                usage.validate_servings(value)

    def test_validate_servings_rejects_nan_infinity_and_absurd_sizes(self) -> None:
        for value in ("nan", "inf", "-inf", "1e400", usage.MAX_SERVINGS + 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                usage.validate_servings(value)

    def test_validate_servings_allows_the_upper_bound(self) -> None:
        self.assertEqual(
            usage.validate_servings(usage.MAX_SERVINGS), usage.MAX_SERVINGS
        )

    def test_calculate_propagates_servings_validation_errors(self) -> None:
        with self.assertRaises(ValueError):
            self._calc("2.5")

    # ------------------------------------------------------------------
    # Seeded state / basic result shape
    # ------------------------------------------------------------------
    def test_seeded_zero_stock_makes_every_line_short(self) -> None:
        result = self._calc(1)
        self.assertFalse(result["sufficient"])
        self.assertEqual(result["shortage_count"], 8)
        self.assertEqual(result["max_servings"], 0)
        self.assertEqual(result["ingredient_count"], 8)
        self.assertEqual(result["menu_item_name"], "Regular Ramen")
        self.assertEqual(
            {row["status"] for row in result["rows"]},
            {usage.STATUS_SHORT},
        )

    def test_rows_are_sorted_by_ingredient_name(self) -> None:
        names = [row["name"] for row in self._calc(1)["rows"]]
        self.assertEqual(names, sorted(names, key=str.lower))

    def test_get_calculator_context_returns_one_recipe_and_eight_lines(self) -> None:
        context = usage.get_calculator_context(db_path=self.db_path)
        self.assertEqual(context["recipe"]["menu_item_name"], "Regular Ramen")
        self.assertEqual(len(context["lines"]), 8)

    def test_calculate_rejects_a_recipe_with_no_lines(self) -> None:
        with database.transaction(self.db_path) as conn:
            conn.execute("DELETE FROM recipe_ingredients")
        with self.assertRaisesRegex(ValueError, "no ingredients"):
            self._calc(1)

    # ------------------------------------------------------------------
    # Requirement maths
    # ------------------------------------------------------------------
    def test_required_quantity_is_per_serving_times_servings(self) -> None:
        result = self._calc(3)
        noodles = self._row(result, "Ramen noodles")
        self.assertEqual(noodles["quantity_per_serving"], 150.0)
        self.assertEqual(noodles["quantity_required"], 450.0)
        broth = self._row(result, "Ramen broth")
        self.assertEqual(broth["quantity_required"], 1200.0)
        egg = self._row(result, "Egg")
        self.assertEqual(egg["quantity_required"], 3.0)

    def test_shortfall_is_required_minus_on_hand(self) -> None:
        result = self._calc(2)
        noodles = self._row(result, "Ramen noodles")
        self.assertEqual(noodles["quantity_on_hand"], 0.0)
        self.assertEqual(noodles["shortfall"], 300.0)
        self.assertEqual(noodles["remaining_after"], -300.0)

    def test_exact_stock_for_n_servings_is_sufficient(self) -> None:
        self._stock_in_for(2)
        result = self._calc(2)
        self.assertTrue(result["sufficient"])
        self.assertEqual(result["shortage_count"], 0)
        self.assertEqual(result["max_servings"], 2)
        self.assertTrue(all(row["shortfall"] == 0 for row in result["rows"]))
        self.assertTrue(
            all(row["remaining_after"] == 0 for row in result["rows"])
        )

    def test_one_serving_too_many_is_refused(self) -> None:
        self._stock_in_all("10000")
        enough = self._calc(25)
        too_many = self._calc(26)
        self.assertTrue(enough["sufficient"])
        self.assertFalse(too_many["sufficient"])
        self.assertEqual(too_many["shortage_count"], 1)
        self.assertEqual(self._row(too_many, "Ramen broth")["shortfall"], 400.0)

    def test_max_servings_is_limited_by_the_scarcest_ingredient(self) -> None:
        self._stock_in_all("10000")
        result = self._calc(1)
        self.assertEqual(result["max_servings"], 25)
        self.assertEqual(result["limiting_ingredients"], ["Ramen broth"])

    def test_get_max_servings_matches_the_calculated_value(self) -> None:
        self._stock_in_all("10000")
        self.assertEqual(
            usage.get_max_servings(db_path=self.db_path),
            self._calc(1)["max_servings"],
        )

    def test_fractional_recipe_quantities_are_handled(self) -> None:
        line = next(
            row for row in rr.list_recipe_lines(db_path=self.db_path)
            if row["name"] == "Garlic"
        )
        rr.update_quantity(
            int(line["recipe_ingredient_id"]), "0.5", db_path=self.db_path
        )
        result = self._calc(4)
        self.assertEqual(self._row(result, "Garlic")["quantity_required"], 2.0)

    def test_recipe_edits_change_the_calculation(self) -> None:
        self._stock_in_all("10000")
        before = self._calc(1)["max_servings"]
        line = next(
            row for row in rr.list_recipe_lines(db_path=self.db_path)
            if row["name"] == "Ramen broth"
        )
        rr.update_quantity(
            int(line["recipe_ingredient_id"]), "800", db_path=self.db_path
        )
        after = self._calc(1)["max_servings"]
        self.assertEqual(before, 25)
        self.assertEqual(after, 12)

    # ------------------------------------------------------------------
    # Stock changes are reflected (calculator reads live balances)
    # ------------------------------------------------------------------
    def test_stock_in_changes_the_verdict(self) -> None:
        self.assertFalse(self._calc(1)["sufficient"])
        self._stock_in_all("10000")
        self.assertTrue(self._calc(1)["sufficient"])

    def test_stock_out_changes_the_verdict_back(self) -> None:
        self._stock_in_for(3)
        self.assertTrue(self._calc(3)["sufficient"])
        noodles = self._raw(
            "SELECT ingredient_id FROM ingredients WHERE name = 'Ramen noodles'"
        )[0]
        stock.record_stock_out(
            [{"ingredient_id": int(noodles[0]), "quantity": "400"}],
            "Consumption", user_id=1, db_path=self.db_path,
        )
        result = self._calc(3)
        self.assertFalse(result["sufficient"])
        self.assertEqual(self._row(result, "Ramen noodles")["shortfall"], 400.0)

    def test_soft_deleting_an_ingredient_removes_its_recipe_line(self) -> None:
        item = repository.list_ingredients("Garlic", db_path=self.db_path)[0]
        repository.delete_ingredient(
            int(item["ingredient_id"]), db_path=self.db_path
        )
        result = self._calc(1)
        self.assertEqual(result["ingredient_count"], 7)
        self.assertNotIn("Garlic", [row["name"] for row in result["rows"]])

    def test_inactive_recipe_ingredient_is_reported_unavailable(self) -> None:
        self._stock_in_all("10000")
        with database.transaction(self.db_path) as conn:
            conn.execute(
                "UPDATE ingredients SET is_active = 0 WHERE name = 'Egg'"
            )
        result = self._calc(1)
        egg = self._row(result, "Egg")
        self.assertEqual(egg["status"], usage.STATUS_UNAVAILABLE)
        self.assertEqual(egg["max_servings"], 0)
        self.assertEqual(result["max_servings"], 0)
        self.assertFalse(result["sufficient"])

    # ------------------------------------------------------------------
    # THE key rule: no deduction, no writes of any kind
    # ------------------------------------------------------------------
    def test_calculate_does_not_change_the_database_file(self) -> None:
        self._stock_in_all("10000")
        before = self._hash()
        for servings in (1, 2, 25, 26, 100):
            self._calc(servings)
        self.assertEqual(self._hash(), before)

    def test_calculate_does_not_change_balances_or_recipe(self) -> None:
        self._stock_in_all("10000")
        before_balances = self._raw(
            "SELECT ingredient_id, quantity_on_hand, updated_at "
            "FROM ingredients ORDER BY ingredient_id"
        )
        before_recipe = self._raw(
            "SELECT recipe_ingredient_id, quantity_per_serving "
            "FROM recipe_ingredients ORDER BY recipe_ingredient_id"
        )
        before_recipe_header = self._raw(
            "SELECT recipe_id, notes, updated_at FROM recipes"
        )
        self._calc(7)
        self.assertEqual(
            self._raw(
                "SELECT ingredient_id, quantity_on_hand, updated_at "
                "FROM ingredients ORDER BY ingredient_id"
            ),
            before_balances,
        )
        self.assertEqual(
            self._raw(
                "SELECT recipe_ingredient_id, quantity_per_serving "
                "FROM recipe_ingredients ORDER BY recipe_ingredient_id"
            ),
            before_recipe,
        )
        self.assertEqual(
            self._raw("SELECT recipe_id, notes, updated_at FROM recipes"),
            before_recipe_header,
        )

    def test_calculate_creates_no_stock_transactions(self) -> None:
        self._stock_in_all("10000")
        before = (
            self._raw("SELECT COUNT(*) FROM stock_in_transactions")[0][0],
            self._raw("SELECT COUNT(*) FROM stock_out_transactions")[0][0],
            self._raw("SELECT COUNT(*) FROM vw_stock_movements")[0][0],
        )
        for _ in range(5):
            self._calc(3)
        self.assertEqual(
            (
                self._raw("SELECT COUNT(*) FROM stock_in_transactions")[0][0],
                self._raw("SELECT COUNT(*) FROM stock_out_transactions")[0][0],
                self._raw("SELECT COUNT(*) FROM vw_stock_movements")[0][0],
            ),
            before,
        )

    def test_calculate_works_on_a_read_only_connection(self) -> None:
        """A mode=ro SQLite connection proves no write is ever attempted."""
        def read_only(_path: Any) -> sqlite3.Connection:
            return sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)

        with patch("database.get_connection", side_effect=read_only):
            result = usage.calculate(2, db_path=self.db_path)
        self.assertEqual(result["ingredient_count"], 8)
        self.assertEqual(result["servings"], 2)

    def test_calculate_never_opens_a_write_transaction(self) -> None:
        with patch(
            "database.transaction",
            side_effect=AssertionError("calculator must not open a write transaction"),
        ):
            result = usage.calculate(1, db_path=self.db_path)
        self.assertEqual(result["ingredient_count"], 8)

    def test_module_source_contains_no_write_sql(self) -> None:
        """Inspect every string literal, not the prose in docstrings."""
        tree = ast.parse(Path(usage.__file__).read_text(encoding="utf-8"))
        literals = [
            " ".join(node.value.strip().split()).lower()
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
        for statement in literals:
            self.assertNotRegex(
                statement,
                r"^(insert|update|delete|replace|create|drop|alter)\b",
                msg=f"Found a write statement: {statement[:60]}",
            )

    def test_dashboard_and_integrity_are_unchanged_after_calculating(self) -> None:
        before = database.get_dashboard_data(db_path=self.db_path)
        self._calc(9)
        self.assertEqual(
            database.get_dashboard_data(db_path=self.db_path), before
        )
        with database.transaction(self.db_path) as conn:
            self.assertEqual(
                conn.execute("PRAGMA integrity_check").fetchone()[0], "ok"
            )
            self.assertEqual(
                conn.execute("PRAGMA foreign_key_check").fetchall(), []
            )

    # ------------------------------------------------------------------
    # Result dictionary completeness
    # ------------------------------------------------------------------
    def test_result_contains_every_key_the_page_needs(self) -> None:
        result = self._calc(2)
        expected = {
            "servings", "recipe_id", "menu_item_name", "recipe_updated_at",
            "ingredient_count", "rows", "sufficient", "shortage_count",
            "shortages", "max_servings", "limiting_ingredients",
        }
        self.assertTrue(expected.issubset(result))
        self.assertEqual(len(result["shortages"]), result["shortage_count"])

    def test_each_row_contains_every_column_the_table_needs(self) -> None:
        row = self._calc(1)["rows"][0]
        expected = {
            "recipe_ingredient_id", "ingredient_id", "name", "unit",
            "quantity_per_serving", "quantity_required", "quantity_on_hand",
            "shortfall", "remaining_after", "max_servings", "is_active",
            "status",
        }
        self.assertTrue(expected.issubset(row))


if __name__ == "__main__":
    unittest.main()
