"""
Stage 1 automated checks - schema, seed data, security and constraints.

Run from the project folder:
    python -m unittest discover -s tests -v

These tests use a temporary SQLite file, so they never touch the real
database/ramen_inventory.db. No GUI is opened.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import database


class Stage1Tests(unittest.TestCase):
    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.db_path = Path(temp_dir.name) / "ramen_inventory.db"
        database.initialize_database(self.db_path)

    def _query(self, sql: str, params: tuple = ()) -> sqlite3.Row:
        with closing(database.get_connection(self.db_path)) as conn:
            return conn.execute(sql, params).fetchone()

    # ------------------------------------------------------------------
    def test_database_module_is_not_shadowed_by_folder(self) -> None:
        self.assertTrue(Path(database.__file__).name == "database.py")

    def test_all_required_tables_and_view_exist(self) -> None:
        with closing(database.get_connection(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT name, type FROM sqlite_master WHERE type IN ('table','view')"
            ).fetchall()
        names = {row["name"] for row in rows}
        required = {
            "users",
            "menu_items",
            "recipes",
            "recipe_ingredients",
            "ingredients",
            "stock_in_transactions",
            "stock_in_details",
            "stock_out_transactions",
            "stock_out_details",
            "vw_stock_movements",
        }
        self.assertTrue(required.issubset(names), f"Missing: {required - names}")

    def test_foreign_keys_are_on_and_enforced(self) -> None:
        self.assertEqual(self._query("PRAGMA foreign_keys")[0], 1)
        with self.assertRaises(sqlite3.IntegrityError):
            with database.transaction(self.db_path) as conn:
                header = conn.execute(
                    "INSERT INTO stock_in_transactions (transaction_date) VALUES (?)",
                    ("2026-11-08 10:00:00",),
                )
                conn.execute(
                    """
                    INSERT INTO stock_in_details
                        (stock_in_id, ingredient_id, quantity, unit)
                    VALUES (?, ?, ?, ?)
                    """,
                    (header.lastrowid, 99999, 10.0, "grams"),
                )

    def test_seed_data_is_correct(self) -> None:
        self.assertEqual(self._query("SELECT COUNT(*) FROM users")[0], 1)
        self.assertEqual(
            self._query("SELECT name FROM menu_items LIMIT 1")[0], "Regular Ramen"
        )
        self.assertEqual(self._query("SELECT COUNT(*) FROM recipes")[0], 1)
        self.assertEqual(
            self._query("SELECT COUNT(*) FROM ingredients WHERE is_active = 1")[0], 8
        )
        self.assertEqual(
            self._query("SELECT COUNT(*) FROM recipe_ingredients")[0], 8
        )
        self.assertEqual(
            self._query("SELECT COUNT(*) FROM vw_stock_movements")[0], 0
        )
        # No stock is invented at seed time; stock-in transactions fill it.
        self.assertEqual(
            self._query("SELECT SUM(quantity_on_hand) FROM ingredients")[0], 0
        )

        recipe = self._query(
            """
            SELECT i.name, i.unit, r.quantity_per_serving
            FROM recipe_ingredients r
            JOIN ingredients i ON i.ingredient_id = r.ingredient_id
            WHERE i.name = ?
            """,
            ("Ramen noodles",),
        )
        self.assertEqual(recipe["unit"], "grams")
        self.assertEqual(recipe["quantity_per_serving"], 150.0)
        self.assertEqual(
            self._query(
                """
                SELECT quantity_per_serving FROM recipe_ingredients r
                JOIN ingredients i ON i.ingredient_id = r.ingredient_id
                WHERE i.name = 'Ramen broth'
                """
            )[0],
            400.0,
        )

    def test_snapshot_is_read_from_sqlite(self) -> None:
        snapshot = database.get_system_snapshot(self.db_path)
        self.assertEqual(snapshot["menu_items"], 1)
        self.assertEqual(snapshot["ingredients"], 8)
        self.assertEqual(snapshot["recipe_lines"], 8)
        self.assertEqual(snapshot["stock_movements"], 0)
        self.assertEqual(snapshot["out_of_stock"], 8)

    # ------------------------------------------------------------------
    def test_second_menu_item_is_impossible(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            with database.transaction(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO menu_items (name, created_at)
                    VALUES (?, ?)
                    """,
                    ("Spicy Ramen", "2026-11-08 10:00:00"),
                )

    def test_duplicate_recipe_ingredient_is_impossible(self) -> None:
        ids = self._query(
            """
            SELECT r.recipe_id, r.ingredient_id
            FROM recipe_ingredients r LIMIT 1
            """
        )
        with self.assertRaises(sqlite3.IntegrityError):
            with database.transaction(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO recipe_ingredients
                        (recipe_id, ingredient_id, quantity_per_serving)
                    VALUES (?, ?, ?)
                    """,
                    (ids["recipe_id"], ids["ingredient_id"], 1.0),
                )

    def test_negative_stock_and_zero_recipe_quantity_are_blocked(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            with database.transaction(self.db_path) as conn:
                conn.execute(
                    "UPDATE ingredients SET quantity_on_hand = -1 WHERE ingredient_id = 1"
                )
        with self.assertRaises(sqlite3.IntegrityError):
            with database.transaction(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO recipe_ingredients
                        (recipe_id, ingredient_id, quantity_per_serving)
                    VALUES (1, 2, 0)
                    """
                )

    def test_soft_deleted_name_can_be_reused(self) -> None:
        with database.transaction(self.db_path) as conn:
            conn.execute(
                "UPDATE ingredients SET is_active = 0 WHERE name = 'Pork'"
            )
            conn.execute(
                """
                INSERT INTO ingredients
                    (name, unit, created_at, updated_at)
                VALUES ('Pork', 'grams', '2026-11-08 10:00:00', '2026-11-08 10:00:00')
                """
            )
        self.assertEqual(
            self._query(
                "SELECT COUNT(*) FROM ingredients WHERE name = 'Pork'"
            )[0],
            2,
        )

    def test_transaction_rolls_back_on_error(self) -> None:
        with self.assertRaises(RuntimeError):
            with database.transaction(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO stock_in_transactions (transaction_date, notes)
                    VALUES (?, ?)
                    """,
                    ("2026-11-08 10:00:00", "should be rolled back"),
                )
                raise RuntimeError("simulated failure")
        self.assertEqual(
            self._query("SELECT COUNT(*) FROM stock_in_transactions")[0], 0
        )

    def test_movement_view_has_signed_quantities_and_reasons(self) -> None:
        with database.transaction(self.db_path) as conn:
            incoming = conn.execute(
                """
                INSERT INTO stock_in_transactions (transaction_date, reference_no)
                VALUES (?, ?)
                """,
                ("2026-11-08 09:00:00", "PO-001"),
            )
            conn.execute(
                """
                INSERT INTO stock_in_details
                    (stock_in_id, ingredient_id, quantity, unit)
                VALUES (?, 1, 1000, 'grams')
                """,
                (incoming.lastrowid,),
            )
            outgoing = conn.execute(
                """
                INSERT INTO stock_out_transactions (transaction_date, reason)
                VALUES (?, ?)
                """,
                ("2026-11-08 12:00:00", "Damaged"),
            )
            conn.execute(
                """
                INSERT INTO stock_out_details
                    (stock_out_id, ingredient_id, quantity, unit)
                VALUES (?, 1, 25, 'grams')
                """,
                (outgoing.lastrowid,),
            )
        with closing(database.get_connection(self.db_path)) as conn:
            rows = conn.execute(
                """
                SELECT movement_type, quantity, reason
                FROM vw_stock_movements ORDER BY movement_type
                """
            ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["movement_type"], "IN")
        self.assertEqual(rows[0]["quantity"], 1000)
        self.assertEqual(rows[1]["movement_type"], "OUT")
        self.assertEqual(rows[1]["quantity"], -25)
        self.assertEqual(rows[1]["reason"], "Damaged")

    # ------------------------------------------------------------------
    def test_password_is_hashed_and_login_works(self) -> None:
        stored = self._query("SELECT password_hash FROM users")[0]
        self.assertNotEqual(stored, database.DEFAULT_ADMIN_PASSWORD)
        self.assertTrue(stored.startswith("pbkdf2_sha256$"))
        self.assertEqual(len(stored.split("$")), 4)

        user = database.verify_credentials(
            "admin", database.DEFAULT_ADMIN_PASSWORD, self.db_path
        )
        self.assertIsNotNone(user)
        assert user is not None
        self.assertNotIn("password_hash", user)
        self.assertEqual(user["must_change_password"], 1)
        self.assertIsNone(
            database.verify_credentials("admin", "wrong-password", self.db_path)
        )
        self.assertIsNone(
            database.verify_credentials("nobody", "wrong-password", self.db_path)
        )
        self.assertTrue(database.default_credentials_active(self.db_path))

    def test_password_change_and_validation(self) -> None:
        user_id = self._query("SELECT user_id FROM users")[0]
        with self.assertRaises(ValueError):
            database.change_password(
                user_id, database.DEFAULT_ADMIN_PASSWORD, "short", self.db_path
            )
        with self.assertRaises(ValueError):
            database.change_password(
                user_id, "not-the-current-password", "NewPass123", self.db_path
            )

        database.change_password(
            user_id, database.DEFAULT_ADMIN_PASSWORD, "NoodleBar2026", self.db_path
        )
        self.assertIsNone(
            database.verify_credentials(
                "admin", database.DEFAULT_ADMIN_PASSWORD, self.db_path
            )
        )
        changed = database.verify_credentials(
            "admin", "NoodleBar2026", self.db_path
        )
        self.assertIsNotNone(changed)
        assert changed is not None
        self.assertEqual(changed["must_change_password"], 0)
        self.assertIsNotNone(changed["last_login_at"])
        self.assertFalse(database.default_credentials_active(self.db_path))

    def test_initialize_is_idempotent(self) -> None:
        user_id = self._query("SELECT user_id FROM users")[0]
        database.change_password(
            user_id, database.DEFAULT_ADMIN_PASSWORD, "NoodleBar2026", self.db_path
        )
        database.initialize_database(self.db_path)
        self.assertEqual(self._query("SELECT COUNT(*) FROM users")[0], 1)
        self.assertEqual(
            self._query("SELECT COUNT(*) FROM ingredients WHERE is_active = 1")[0], 8
        )
        self.assertEqual(
            self._query("SELECT COUNT(*) FROM recipe_ingredients")[0], 8
        )
        self.assertIsNotNone(
            database.verify_credentials("admin", "NoodleBar2026", self.db_path)
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
