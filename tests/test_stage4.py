"""
Stage 4 automated checks - the Regular Ramen recipe is really editable in
SQLite, and editing it never touches stock or transaction history.

Run from the project folder:
    python -m unittest discover -s tests -v

Uses a temporary database; the real database/ramen_inventory.db is untouched.
No GUI is opened.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import database
import recipe_repository as rr
import repository


class Stage4RecipeTests(unittest.TestCase):
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

    def _line(self, name: str) -> dict:
        for line in rr.list_recipe_lines(self.db_path):
            if line["name"] == name:
                return line
        raise AssertionError(f"No recipe line for {name!r}")

    def _new_catalog_ingredient(self, name: str = "Bamboo shoots") -> int:
        return repository.add_ingredient(name, "grams", 5, "", self.db_path)

    def _old_timestamp(self) -> None:
        """Make the 'last updated' assertions unambiguous."""
        with database.transaction(self.db_path) as conn:
            conn.execute("UPDATE recipes SET updated_at = '2000-01-01 00:00:00'")
            conn.execute(
                "UPDATE ingredients SET updated_at = '2000-01-01 00:00:00'"
            )

    # ------------------------------------------------------------------
    # Seeded recipe / reads
    # ------------------------------------------------------------------
    def test_seeded_recipe_has_the_eight_required_quantities(self) -> None:
        lines = rr.list_recipe_lines(self.db_path)
        self.assertEqual(len(lines), 8)
        actual = {line["name"]: line["quantity_per_serving"] for line in lines}
        expected = {
            "Ramen noodles": 150.0,
            "Pork": 100.0,
            "Ramen broth": 400.0,
            "Egg": 1.0,
            "Nori sheets": 1.0,
            "Green onions": 10.0,
            "Soy sauce": 15.0,
            "Garlic": 5.0,
        }
        self.assertEqual(actual, expected)

    def test_units_come_from_the_ingredient_catalog(self) -> None:
        units = {
            line["name"]: line["unit"] for line in rr.list_recipe_lines(self.db_path)
        }
        self.assertEqual(units["Ramen noodles"], "grams")
        self.assertEqual(units["Ramen broth"], "ml")
        self.assertEqual(units["Egg"], "piece")
        self.assertEqual(units["Nori sheets"], "sheet")

    def test_recipe_summary_counts_are_read_from_sqlite(self) -> None:
        recipe = rr.get_recipe(self.db_path)
        self.assertEqual(recipe["menu_item_name"], "Regular Ramen")
        self.assertEqual(recipe["ingredient_count"], 8)
        self.assertEqual(recipe["unit_count"], 4)
        self.assertEqual(recipe["notes"], "Sample recipe - quantities are editable.")
        self.assertTrue(recipe["updated_at"])

    def test_only_one_menu_item_and_one_recipe_can_exist(self) -> None:
        self.assertEqual(self._raw("SELECT COUNT(*) FROM menu_items")[0][0], 1)
        self.assertEqual(self._raw("SELECT COUNT(*) FROM recipes")[0][0], 1)
        with self.assertRaises(sqlite3.IntegrityError):
            with database.transaction(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO menu_items (name, created_at) VALUES (?, ?)",
                    ("Second ramen", "2026-11-09 10:00:00"),
                )

    def test_recipe_line_rows_include_derived_stock_status(self) -> None:
        for line in rr.list_recipe_lines(self.db_path):
            self.assertEqual(line["status"], "Out of stock")
            self.assertEqual(line["quantity_on_hand"], 0)
            self.assertEqual(line["is_active"], 1)

    def test_no_available_ingredients_initially(self) -> None:
        self.assertEqual(rr.list_available_ingredients(self.db_path), [])

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def test_validate_quantity_rejects_bad_input(self) -> None:
        for bad in ("", "   ", "abc", "0", "-1", "nan", "inf", "1e309",
                    "1000001"):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    rr.validate_quantity(bad)

    def test_validate_quantity_normalises_valid_input(self) -> None:
        self.assertEqual(rr.validate_quantity(" 12.5 "), 12.5)
        self.assertEqual(rr.validate_quantity(150), 150.0)
        self.assertEqual(rr.validate_quantity("0.25"), 0.25)

    def test_validate_notes(self) -> None:
        self.assertEqual(rr.validate_notes("  Simmer broth  "), "Simmer broth")
        self.assertEqual(rr.validate_notes(None), "")
        with self.assertRaisesRegex(ValueError, "200 characters"):
            rr.validate_notes("x" * 201)

    # ------------------------------------------------------------------
    # Edit quantity
    # ------------------------------------------------------------------
    def test_update_quantity_persists_and_updates_recipe_timestamp(self) -> None:
        self._old_timestamp()
        line = self._line("Ramen noodles")
        result = rr.update_quantity(
            line["recipe_ingredient_id"], "175.5", self.db_path
        )
        self.assertEqual(result["name"], "Ramen noodles")
        self.assertEqual(result["quantity_per_serving"], 175.5)
        self.assertEqual(
            self._line("Ramen noodles")["quantity_per_serving"], 175.5
        )
        # A fresh connection must see the new value (the write was committed).
        self.assertEqual(
            self._raw(
                "SELECT quantity_per_serving FROM recipe_ingredients "
                "WHERE recipe_ingredient_id = ?",
                (line["recipe_ingredient_id"],),
            )[0][0],
            175.5,
        )
        self.assertNotEqual(
            rr.get_recipe(self.db_path)["updated_at"], "2000-01-01 00:00:00"
        )

    def test_update_quantity_never_changes_stock(self) -> None:
        self._old_timestamp()
        line = self._line("Pork")
        with database.transaction(self.db_path) as conn:
            conn.execute(
                "UPDATE ingredients SET quantity_on_hand = 750 "
                "WHERE ingredient_id = ?",
                (line["ingredient_id"],),
            )
        rr.update_quantity(line["recipe_ingredient_id"], "125", self.db_path)
        self.assertEqual(self._line("Pork")["quantity_on_hand"], 750)
        self.assertEqual(
            self._raw(
                "SELECT updated_at FROM ingredients WHERE ingredient_id = ?",
                (line["ingredient_id"],),
            )[0][0],
            "2000-01-01 00:00:00",
        )

    def test_update_quantity_rejects_invalid_value_or_missing_line(self) -> None:
        line = self._line("Egg")
        with self.assertRaises(ValueError):
            rr.update_quantity(line["recipe_ingredient_id"], "-3", self.db_path)
        with self.assertRaises(ValueError):
            rr.update_quantity(9999, "10", self.db_path)
        self.assertEqual(self._line("Egg")["quantity_per_serving"], 1.0)

    # ------------------------------------------------------------------
    # Add ingredient
    # ------------------------------------------------------------------
    def test_add_catalog_ingredient_to_recipe(self) -> None:
        self._old_timestamp()
        new_id = self._new_catalog_ingredient()
        self.assertEqual(
            [row["name"] for row in rr.list_available_ingredients(self.db_path)],
            ["Bamboo shoots"],
        )
        result = rr.add_ingredient(new_id, "25", self.db_path)
        self.assertEqual(result["name"], "Bamboo shoots")
        self.assertEqual(result["unit"], "grams")
        self.assertEqual(rr.get_recipe(self.db_path)["ingredient_count"], 9)
        self.assertEqual(self._line("Bamboo shoots")["quantity_per_serving"], 25)
        self.assertEqual(rr.list_available_ingredients(self.db_path), [])
        self.assertEqual(
            self._raw("SELECT COUNT(*) FROM recipe_ingredients")[0][0], 9
        )
        self.assertNotEqual(
            rr.get_recipe(self.db_path)["updated_at"], "2000-01-01 00:00:00"
        )

    def test_add_rejects_duplicate_recipe_line(self) -> None:
        pork = self._line("Pork")
        with self.assertRaisesRegex(ValueError, "already in the recipe"):
            rr.add_ingredient(pork["ingredient_id"], "50", self.db_path)
        self.assertEqual(rr.get_recipe(self.db_path)["ingredient_count"], 8)

    def test_schema_unique_constraint_also_blocks_duplicates(self) -> None:
        pork = self._line("Pork")
        with self.assertRaises(sqlite3.IntegrityError):
            with database.transaction(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO recipe_ingredients "
                    "(recipe_id, ingredient_id, quantity_per_serving) "
                    "VALUES (?, ?, ?)",
                    (pork["recipe_id"], pork["ingredient_id"], 10),
                )

    def test_add_rejects_unknown_or_inactive_ingredient(self) -> None:
        with self.assertRaisesRegex(ValueError, "no longer exists"):
            rr.add_ingredient(9999, "10", self.db_path)
        new_id = self._new_catalog_ingredient()
        repository.delete_ingredient(new_id, self.db_path)
        with self.assertRaisesRegex(ValueError, "Only active ingredients"):
            rr.add_ingredient(new_id, "10", self.db_path)
        self.assertEqual(rr.get_recipe(self.db_path)["ingredient_count"], 8)

    def test_add_rejects_bad_quantity_before_writing(self) -> None:
        new_id = self._new_catalog_ingredient()
        for bad in ("", "0", "abc"):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    rr.add_ingredient(new_id, bad, self.db_path)
        self.assertEqual(rr.get_recipe(self.db_path)["ingredient_count"], 8)
        self.assertEqual(len(rr.list_available_ingredients(self.db_path)), 1)

    # ------------------------------------------------------------------
    # Remove ingredient
    # ------------------------------------------------------------------
    def test_remove_line_keeps_ingredient_and_stock_intact(self) -> None:
        self._old_timestamp()
        garlic = self._line("Garlic")
        with database.transaction(self.db_path) as conn:
            conn.execute(
                "UPDATE ingredients SET quantity_on_hand = 40 "
                "WHERE ingredient_id = ?",
                (garlic["ingredient_id"],),
            )
        result = rr.remove_ingredient(
            garlic["recipe_ingredient_id"], self.db_path
        )
        self.assertEqual(result["name"], "Garlic")
        self.assertEqual(rr.get_recipe(self.db_path)["ingredient_count"], 7)
        self.assertEqual(repository.count_active(self.db_path), 8)
        self.assertEqual(
            repository.get_ingredient(garlic["ingredient_id"], self.db_path)[
                "quantity_on_hand"
            ],
            40,
        )
        self.assertEqual(
            len(rr.list_available_ingredients(self.db_path)), 1
        )
        self.assertNotEqual(
            rr.get_recipe(self.db_path)["updated_at"], "2000-01-01 00:00:00"
        )

    def test_last_recipe_line_cannot_be_removed(self) -> None:
        for _ in range(7):
            line = rr.list_recipe_lines(self.db_path)[0]
            rr.remove_ingredient(line["recipe_ingredient_id"], self.db_path)
        self.assertEqual(len(rr.list_recipe_lines(self.db_path)), 1)
        last = rr.list_recipe_lines(self.db_path)[0]
        with self.assertRaisesRegex(ValueError, "at least one ingredient"):
            rr.remove_ingredient(last["recipe_ingredient_id"], self.db_path)
        self.assertEqual(len(rr.list_recipe_lines(self.db_path)), 1)

    def test_remove_missing_line_raises(self) -> None:
        with self.assertRaises(ValueError):
            rr.remove_ingredient(9999, self.db_path)

    # ------------------------------------------------------------------
    # Notes
    # ------------------------------------------------------------------
    def test_update_notes_persists_and_can_be_cleared(self) -> None:
        self._old_timestamp()
        rr.update_notes("  Simmer broth for 6 hours  ", self.db_path)
        self.assertEqual(rr.get_recipe(self.db_path)["notes"], "Simmer broth for 6 hours")
        self.assertEqual(
            self._raw("SELECT notes FROM recipes")[0][0],
            "Simmer broth for 6 hours",
        )
        self.assertNotEqual(
            rr.get_recipe(self.db_path)["updated_at"], "2000-01-01 00:00:00"
        )
        self.assertEqual(rr.update_notes("", self.db_path), "")
        self.assertEqual(rr.get_recipe(self.db_path)["notes"], "")

    def test_update_notes_rejects_overlong_text(self) -> None:
        with self.assertRaises(ValueError):
            rr.update_notes("x" * 201, self.db_path)
        self.assertEqual(
            rr.get_recipe(self.db_path)["notes"],
            "Sample recipe - quantities are editable.",
        )

    # ------------------------------------------------------------------
    # Cross-stage guarantees
    # ------------------------------------------------------------------
    def test_recipe_edits_create_no_stock_movements(self) -> None:
        new_id = self._new_catalog_ingredient()
        rr.update_quantity(
            self._line("Pork")["recipe_ingredient_id"], "120", self.db_path
        )
        rr.add_ingredient(new_id, "20", self.db_path)
        rr.remove_ingredient(
            self._line("Garlic")["recipe_ingredient_id"], self.db_path
        )
        rr.update_notes("Stage 4 test", self.db_path)

        self.assertEqual(
            self._raw("SELECT COUNT(*) FROM stock_in_transactions")[0][0], 0
        )
        self.assertEqual(
            self._raw("SELECT COUNT(*) FROM stock_out_transactions")[0][0], 0
        )
        self.assertEqual(
            self._raw("SELECT COUNT(*) FROM vw_stock_movements")[0][0], 0
        )
        self.assertEqual(
            self._raw("SELECT SUM(quantity_on_hand) FROM ingredients")[0][0], 0
        )

    def test_stage3_soft_delete_also_removes_its_recipe_line(self) -> None:
        pork = self._line("Pork")
        repository.delete_ingredient(pork["ingredient_id"], self.db_path)
        self.assertEqual(rr.get_recipe(self.db_path)["ingredient_count"], 7)
        self.assertEqual(len(rr.list_recipe_lines(self.db_path)), 7)
        self.assertEqual(
            database.get_system_snapshot(self.db_path)["recipe_lines"], 7
        )
        self.assertEqual(rr.list_available_ingredients(self.db_path), [])

    def test_system_snapshot_recipe_lines_follow_recipe_crud(self) -> None:
        self.assertEqual(
            database.get_system_snapshot(self.db_path)["recipe_lines"], 8
        )
        rr.add_ingredient(self._new_catalog_ingredient(), "20", self.db_path)
        self.assertEqual(
            database.get_system_snapshot(self.db_path)["recipe_lines"], 9
        )

    def test_integrity_and_foreign_keys_after_all_recipe_operations(self) -> None:
        new_id = self._new_catalog_ingredient()
        rr.add_ingredient(new_id, "20", self.db_path)
        rr.update_quantity(
            self._line("Egg")["recipe_ingredient_id"], "2", self.db_path
        )
        rr.remove_ingredient(
            self._line("Garlic")["recipe_ingredient_id"], self.db_path
        )
        rr.update_notes("All good", self.db_path)
        self.assertEqual(self._raw("PRAGMA integrity_check")[0][0], "ok")
        self.assertEqual(self._raw("PRAGMA foreign_key_check"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
