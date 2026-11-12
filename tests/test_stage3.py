"""
Stage 3 automated checks - ingredient CRUD really persists in SQLite.

Run from the project folder:
    python -m unittest discover -s tests -v

Uses a temporary database; the real database/ramen_inventory.db is untouched.
No GUI is opened.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import database
import repository


class Stage3IngredientTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.db_path = Path(temp_dir.name) / "ramen_inventory.db"
        database.initialize_database(self.db_path)

    def _raw(self, sql: str, params: tuple = ()) -> list[tuple]:
        # Connections use sqlite3.Row; normalise to tuples for simple asserts.
        with database.transaction(self.db_path) as conn:
            return [tuple(row) for row in conn.execute(sql, params).fetchall()]

    def _pork(self) -> dict:
        rows = repository.list_ingredients("Pork", self.db_path)
        return rows[0]

    # ------------------------------------------------------------------
    # Reads / search
    # ------------------------------------------------------------------
    def test_seeded_eight_ingredients_are_listed(self) -> None:
        rows = repository.list_ingredients(db_path=self.db_path)
        self.assertEqual(len(rows), 8)
        self.assertEqual(repository.count_active(self.db_path), 8)
        names = {row["name"] for row in rows}
        self.assertIn("Ramen noodles", names)
        self.assertIn("Green onions", names)

    def test_each_row_has_derived_status_fields(self) -> None:
        for row in repository.list_ingredients(db_path=self.db_path):
            self.assertEqual(row["status"], "Out of stock")
            self.assertEqual(row["expiry_status"], "Not set")
            self.assertEqual(row["quantity_on_hand"], 0)

    def test_search_matches_name_and_unit_case_insensitively(self) -> None:
        self.assertEqual(
            [r["name"] for r in repository.list_ingredients("PORK", self.db_path)],
            ["Pork"],
        )
        units = repository.list_ingredients("ml", self.db_path)
        self.assertEqual({r["name"] for r in units}, {"Ramen broth", "Soy sauce"})
        self.assertEqual(repository.list_ingredients("zzzz", self.db_path), [])

    def test_search_treats_like_wildcards_as_literal_text(self) -> None:
        self.assertEqual(repository.list_ingredients("%", self.db_path), [])
        self.assertEqual(repository.list_ingredients("_", self.db_path), [])

    def test_get_ingredient_and_unit_choices(self) -> None:
        pork = self._pork()
        self.assertEqual(
            repository.get_ingredient(pork["ingredient_id"], self.db_path)["name"],
            "Pork",
        )
        self.assertEqual(
            repository.get_unit_choices(self.db_path),
            ["grams", "ml", "piece", "sheet"],
        )

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def test_validation_rejects_bad_input(self) -> None:
        bad_cases = [
            ("", "grams", "1", ""),
            ("X" * 61, "grams", "1", ""),
            ("Valid", "", "1", ""),
            ("Valid", "grams", "", ""),
            ("Valid", "grams", "-2", ""),
            ("Valid", "grams", "abc", ""),
            ("Valid", "grams", "nan", ""),
            ("Valid", "grams", "1", "09/11/2026"),
            ("Valid", "grams", "1", "2026-02-30"),
            ("Valid", "grams", "1", "2026-1-1"),
        ]
        for case in bad_cases:
            with self.subTest(case=case):
                with self.assertRaises(ValueError):
                    repository.validate(*case)

    def test_validation_normalises_valid_input(self) -> None:
        clean = repository.validate("  Nori sheets  ", " sheet ", " 2.5 ", "")
        self.assertEqual(clean["name"], "Nori sheets")
        self.assertEqual(clean["unit"], "sheet")
        self.assertEqual(clean["minimum_stock"], 2.5)
        self.assertIsNone(clean["expiration_date"])
        self.assertEqual(
            repository.validate("A", "g", "0", "2026-12-01")["expiration_date"],
            "2026-12-01",
        )

    def test_stock_status_rule(self) -> None:
        self.assertEqual(repository.stock_status(0, 0), "Out of stock")
        self.assertEqual(repository.stock_status(5, 10), "Low stock")
        self.assertEqual(repository.stock_status(11, 10), "In stock")

    def test_expiry_status_classification(self) -> None:
        today = date(2026, 11, 9)
        self.assertEqual(repository.expiry_status(None, today), "Not set")
        self.assertEqual(
            repository.expiry_status((today - timedelta(days=1)).isoformat(), today),
            "Expired",
        )
        self.assertEqual(
            repository.expiry_status((today + timedelta(days=3)).isoformat(), today),
            "Near expiry",
        )
        self.assertEqual(
            repository.expiry_status((today + timedelta(days=30)).isoformat(), today),
            "OK",
        )

    # ------------------------------------------------------------------
    # Add
    # ------------------------------------------------------------------
    def test_add_creates_row_with_zero_stock_and_persists(self) -> None:
        new_id = repository.add_ingredient(
            "Bamboo shoots", "grams", 20, "2026-12-31", self.db_path
        )
        row = repository.get_ingredient(new_id, self.db_path)
        self.assertEqual(row["name"], "Bamboo shoots")
        self.assertEqual(row["quantity_on_hand"], 0)
        self.assertEqual(row["minimum_stock"], 20)
        self.assertEqual(row["expiration_date"], "2026-12-31")
        self.assertEqual(row["is_active"], 1)
        self.assertEqual(repository.count_active(self.db_path), 9)
        # A fresh connection sees the row: it was committed.
        self.assertEqual(self._raw("SELECT COUNT(*) FROM ingredients")[0][0], 9)

    def test_add_rejects_duplicate_active_name_case_insensitively(self) -> None:
        with self.assertRaisesRegex(ValueError, "already exists"):
            repository.add_ingredient("pORK", "grams", 0, "", self.db_path)
        self.assertEqual(repository.count_active(self.db_path), 8)

    def test_add_rejects_invalid_values_before_writing(self) -> None:
        with self.assertRaises(ValueError):
            repository.add_ingredient("", "grams", 0, "", self.db_path)
        self.assertEqual(repository.count_active(self.db_path), 8)

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------
    def test_update_changes_catalog_fields_only(self) -> None:
        pork = self._pork()
        # Simulate stock that will arrive in Stage 5: it must survive an edit.
        with database.transaction(self.db_path) as conn:
            conn.execute(
                "UPDATE ingredients SET quantity_on_hand = 750 "
                "WHERE ingredient_id = ?",
                (pork["ingredient_id"],),
            )
        repository.update_ingredient(
            pork["ingredient_id"], "Pork belly", "kg", 12.5, "2026-11-20",
            self.db_path,
        )
        updated = repository.get_ingredient(pork["ingredient_id"], self.db_path)
        self.assertEqual(updated["name"], "Pork belly")
        self.assertEqual(updated["unit"], "kg")
        self.assertEqual(updated["minimum_stock"], 12.5)
        self.assertEqual(updated["expiration_date"], "2026-11-20")
        self.assertEqual(updated["quantity_on_hand"], 750)

    def test_update_rejects_duplicate_name_of_another_row(self) -> None:
        pork = self._pork()
        with self.assertRaisesRegex(ValueError, "already exists"):
            repository.update_ingredient(
                pork["ingredient_id"], "Egg", "grams", 0, "", self.db_path
            )
        self.assertEqual(self._pork()["name"], "Pork")

    def test_update_allows_keeping_its_own_name(self) -> None:
        pork = self._pork()
        repository.update_ingredient(
            pork["ingredient_id"], "pork", "grams", 5, "", self.db_path
        )
        self.assertEqual(
            repository.get_ingredient(
                pork["ingredient_id"], self.db_path
            )["name"],
            "pork",
        )

    def test_update_missing_ingredient_raises(self) -> None:
        with self.assertRaises(ValueError):
            repository.update_ingredient(9999, "Ghost", "grams", 0, "", self.db_path)

    # ------------------------------------------------------------------
    # Soft delete
    # ------------------------------------------------------------------
    def test_delete_is_soft_and_preserves_history_foreign_keys(self) -> None:
        pork = self._pork()
        ingredient_id = pork["ingredient_id"]
        with database.transaction(self.db_path) as conn:
            conn.execute(
                "UPDATE ingredients SET quantity_on_hand = 300 "
                "WHERE ingredient_id = ?",
                (ingredient_id,),
            )
            header = conn.execute(
                "INSERT INTO stock_in_transactions (transaction_date, notes) "
                "VALUES ('2026-11-08 09:00:00', 'delivery')"
            )
            conn.execute(
                "INSERT INTO stock_in_details "
                "(stock_in_id, ingredient_id, quantity, unit) VALUES (?, ?, 300, 'grams')",
                (header.lastrowid, ingredient_id),
            )

        result = repository.delete_ingredient(ingredient_id, self.db_path)

        self.assertEqual(result["name"], "Pork")
        self.assertEqual(result["quantity_on_hand"], 300)
        self.assertEqual(result["recipe_links_removed"], 1)
        self.assertEqual(repository.count_active(self.db_path), 7)
        self.assertNotIn(
            "Pork", [r["name"] for r in repository.list_ingredients(db_path=self.db_path)]
        )
        # The physical row, its balance and its transaction detail survive.
        self.assertEqual(
            self._raw(
                "SELECT is_active, quantity_on_hand FROM ingredients "
                "WHERE ingredient_id = ?", (ingredient_id,)
            )[0],
            (0, 300),
        )
        self.assertEqual(
            self._raw("SELECT COUNT(*) FROM vw_stock_movements")[0][0], 1
        )
        self.assertEqual(
            self._raw(
                "SELECT COUNT(*) FROM recipe_ingredients WHERE ingredient_id = ?",
                (ingredient_id,),
            )[0][0],
            0,
        )
        self.assertEqual(
            self._raw("PRAGMA foreign_key_check"), []
        )

    def test_deleted_name_can_be_reused(self) -> None:
        pork = self._pork()
        repository.delete_ingredient(pork["ingredient_id"], self.db_path)
        new_id = repository.add_ingredient("Pork", "grams", 3, "", self.db_path)
        self.assertNotEqual(new_id, pork["ingredient_id"])
        self.assertEqual(repository.count_active(self.db_path), 8)

    def test_delete_twice_raises_and_delete_missing_raises(self) -> None:
        pork = self._pork()
        repository.delete_ingredient(pork["ingredient_id"], self.db_path)
        with self.assertRaises(ValueError):
            repository.delete_ingredient(pork["ingredient_id"], self.db_path)
        with self.assertRaises(ValueError):
            repository.delete_ingredient(9999, self.db_path)

    # ------------------------------------------------------------------
    # Cross-check with the dashboard (no hardcoded statistics)
    # ------------------------------------------------------------------
    def test_dashboard_reacts_to_crud_changes(self) -> None:
        before = database.get_dashboard_data(self.db_path)
        self.assertEqual(before["total_ingredients"], 8)
        repository.add_ingredient("Bamboo shoots", "grams", 5, "", self.db_path)
        mid = database.get_dashboard_data(self.db_path)
        self.assertEqual(mid["total_ingredients"], 9)
        self.assertEqual(mid["out_of_stock"], 9)

        pork = self._pork()
        repository.delete_ingredient(pork["ingredient_id"], self.db_path)
        after = database.get_dashboard_data(self.db_path)
        self.assertEqual(after["total_ingredients"], 8)
        self.assertEqual(after["out_of_stock"], 8)

    def test_database_integrity_after_all_operations(self) -> None:
        repository.add_ingredient("Bamboo shoots", "grams", 5, "", self.db_path)
        pork = self._pork()
        repository.update_ingredient(
            pork["ingredient_id"], "Pork belly", "grams", 5, "2026-11-20",
            self.db_path,
        )
        repository.delete_ingredient(pork["ingredient_id"], self.db_path)
        self.assertEqual(
            self._raw("PRAGMA integrity_check")[0][0], "ok"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
