"""
Stage 2 automated checks - dashboard data really comes from SQLite.

Run from the project folder:
    python -m unittest discover -s tests -v

Uses a temporary database; the real database/ramen_inventory.db is untouched.
No GUI is opened.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import database


class Stage2DashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.db_path = Path(temp_dir.name) / "ramen_inventory.db"
        database.initialize_database(self.db_path)

    def _add_movement(
        self,
        ingredient_name: str,
        quantity: float,
        movement: str = "IN",
        date: str = "2026-11-08 09:00:00",
        reason: str = "Damaged",
    ) -> None:
        """Insert a raw movement row (Stage 5 will add the proper API)."""
        with database.transaction(self.db_path) as conn:
            ingredient_id = conn.execute(
                "SELECT ingredient_id FROM ingredients WHERE name = ?",
                (ingredient_name,),
            ).fetchone()[0]
            if movement == "IN":
                header = conn.execute(
                    """
                    INSERT INTO stock_in_transactions (transaction_date, notes)
                    VALUES (?, ?)
                    """,
                    (date, "test delivery"),
                )
                conn.execute(
                    """
                    INSERT INTO stock_in_details
                        (stock_in_id, ingredient_id, quantity, unit)
                    VALUES (?, ?, ?, 'grams')
                    """,
                    (header.lastrowid, ingredient_id, quantity),
                )
            else:
                header = conn.execute(
                    """
                    INSERT INTO stock_out_transactions (transaction_date, reason)
                    VALUES (?, ?)
                    """,
                    (date, reason),
                )
                conn.execute(
                    """
                    INSERT INTO stock_out_details
                        (stock_out_id, ingredient_id, quantity, unit)
                    VALUES (?, ?, ?, 'grams')
                    """,
                    (header.lastrowid, ingredient_id, quantity),
                )

    # ------------------------------------------------------------------
    def test_menu_item_name_is_read_from_sqlite(self) -> None:
        self.assertEqual(database.get_menu_item_name(self.db_path), "Regular Ramen")

    def test_dashboard_defaults_match_seeded_rows(self) -> None:
        data = database.get_dashboard_data(self.db_path)
        self.assertEqual(data["menu_item"], "Regular Ramen")
        self.assertEqual(data["total_ingredients"], 8)
        self.assertEqual(data["low_stock"], 0)
        self.assertEqual(data["out_of_stock"], 8)
        self.assertEqual(data["recent_activities"], [])
        self.assertEqual(len(data["ingredients"]), 8)
        self.assertTrue(
            all(item["status"] == "Out of stock" for item in data["ingredients"])
        )

    def test_stats_are_not_hardcoded(self) -> None:
        with database.transaction(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO ingredients
                    (name, unit, quantity_on_hand, minimum_stock,
                     created_at, updated_at)
                VALUES ('Bamboo shoots', 'grams', 0, 20, '2026-11-08', '2026-11-08')
                """
            )
        data = database.get_dashboard_data(self.db_path)
        self.assertEqual(data["total_ingredients"], 9)
        self.assertEqual(data["out_of_stock"], 9)

    def test_status_rule_and_counts_react_to_stock_changes(self) -> None:
        with database.transaction(self.db_path) as conn:
            conn.execute(
                "UPDATE ingredients SET quantity_on_hand = 100 WHERE name = 'Garlic'"
            )
            conn.execute(
                "UPDATE ingredients SET quantity_on_hand = 5 WHERE name = 'Soy sauce'"
            )
            conn.execute(
                "UPDATE ingredients SET quantity_on_hand = 300 WHERE name = 'Pork'"
            )
        data = database.get_dashboard_data(self.db_path)
        self.assertEqual(data["total_ingredients"], 8)
        self.assertEqual(data["low_stock"], 1)
        self.assertEqual(data["out_of_stock"], 5)

        statuses = {item["name"]: item["status"] for item in data["ingredients"]}
        self.assertEqual(statuses["Garlic"], "In stock")
        self.assertEqual(statuses["Pork"], "In stock")
        self.assertEqual(statuses["Soy sauce"], "Low stock")
        self.assertEqual(statuses["Ramen noodles"], "Out of stock")

    def test_low_and_out_are_mutually_exclusive(self) -> None:
        # A zero balance must never be counted as both low and out of stock.
        data = database.get_dashboard_data(self.db_path)
        self.assertEqual(data["low_stock"] + data["out_of_stock"], 8)

    def test_recent_activities_come_from_the_view(self) -> None:
        self._add_movement("Pork", 250, "IN", "2026-11-08 09:00:00")
        self._add_movement("Egg", 2, "OUT", "2026-11-08 10:30:00", "Expired")

        data = database.get_dashboard_data(self.db_path)
        rows = data["recent_activities"]
        self.assertEqual(len(rows), 2)
        by_type = {row["movement_type"]: row for row in rows}
        self.assertEqual(by_type["IN"]["ingredient_name"], "Pork")
        self.assertEqual(by_type["IN"]["quantity"], 250)
        self.assertIsNone(by_type["IN"]["reason"])
        self.assertEqual(by_type["OUT"]["quantity"], -2)
        self.assertEqual(by_type["OUT"]["reason"], "Expired")
        self.assertEqual(rows[0]["movement_type"], "OUT")  # newest first

    def test_activity_limit_and_ordering(self) -> None:
        for day in range(1, 8):
            self._add_movement(
                "Pork", 10, "IN", f"2026-11-{day:02d} 08:00:00"
            )
        rows = database.get_dashboard_data(
            self.db_path, activity_limit=3
        )["recent_activities"]
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["transaction_date"], "2026-11-07 08:00:00")
        self.assertEqual(rows[2]["transaction_date"], "2026-11-05 08:00:00")


if __name__ == "__main__":
    unittest.main(verbosity=2)
