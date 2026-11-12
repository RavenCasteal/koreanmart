"""
Stage 5 automated checks - stock-in / stock-out / inventory history.

Run from the project folder:
    python -m unittest discover -s tests -v

Uses a temporary database; the real database/ramen_inventory.db is untouched.
No GUI is opened. The important guarantees here are atomicity and the fact
that a balance can NEVER become negative.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any

import database
import recipe_repository as rr
import repository
import stock_repository as stock


class Stage5StockTests(unittest.TestCase):
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

    def _id(self, name: str) -> int:
        rows = self._raw(
            "SELECT ingredient_id FROM ingredients WHERE name = ?", (name,)
        )
        return int(rows[0][0])

    def _balance(self, name: str) -> float:
        return float(
            self._raw(
                "SELECT quantity_on_hand FROM ingredients WHERE name = ?", (name,)
            )[0][0]
        )

    def _line(self, name: str, quantity: Any, **extra: Any) -> dict:
        line = {"ingredient_id": self._id(name), "quantity": quantity}
        line.update(extra)
        return line

    def _stock_in(self, lines: list[dict], **kwargs: Any) -> dict:
        return stock.record_stock_in(
            lines, user_id=kwargs.pop("user_id", 1), db_path=self.db_path, **kwargs
        )

    def _stock_out(self, lines: list[dict], reason: str = "Consumption",
                   **kwargs: Any) -> dict:
        return stock.record_stock_out(
            lines,
            reason,
            user_id=kwargs.pop("user_id", 1),
            db_path=self.db_path,
            **kwargs,
        )

    def _counts(self) -> tuple[int, int, int]:
        return (
            self._raw("SELECT COUNT(*) FROM stock_in_transactions")[0][0],
            self._raw("SELECT COUNT(*) FROM stock_out_transactions")[0][0],
            self._raw("SELECT COUNT(*) FROM vw_stock_movements")[0][0],
        )

    # ------------------------------------------------------------------
    # Seed / reads
    # ------------------------------------------------------------------
    def test_seeded_balances_are_zero_and_no_transactions_exist(self) -> None:
        self.assertEqual(self._raw("SELECT SUM(quantity_on_hand) FROM ingredients")[0][0], 0)
        self.assertEqual(self._counts(), (0, 0, 0))
        self.assertEqual(
            self._raw("SELECT COUNT(*) FROM stock_in_details")[0][0], 0
        )
        self.assertEqual(
            self._raw("SELECT COUNT(*) FROM stock_out_details")[0][0], 0
        )

    def test_list_stock_ingredients_reuses_active_catalog(self) -> None:
        rows = stock.list_stock_ingredients(self.db_path)
        self.assertEqual(len(rows), 8)
        self.assertEqual(
            {row["status"] for row in rows}, {"Out of stock"}
        )
        self.assertTrue(all(row["unit"] for row in rows))

    def test_inventory_summary_is_read_from_sqlite(self) -> None:
        summary = stock.get_inventory_summary(self.db_path)
        self.assertEqual(summary["active_ingredients"], 8)
        self.assertEqual(summary["with_stock"], 0)
        self.assertEqual(summary["low_stock"], 0)
        self.assertEqual(summary["out_of_stock"], 8)
        self.assertEqual(summary["total_movements"], 0)
        self.assertEqual(summary["last_movement"], "—")

    def test_empty_history_snapshot(self) -> None:
        data = stock.get_history(db_path=self.db_path)
        self.assertEqual(data["rows"], [])
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["filtered"], 0)
        self.assertEqual(data["stock_in_lines"], 0)
        self.assertEqual(data["stock_out_lines"], 0)
        self.assertEqual(stock.get_recent_movements(5, self.db_path), [])

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def test_validate_quantity_rejects_bad_input(self) -> None:
        for bad in ("", "   ", "abc", "0", "-1", "nan", "inf", "1e309",
                    "1000001"):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    stock.validate_quantity(bad)

    def test_validate_quantity_normalises_valid_input(self) -> None:
        self.assertEqual(stock.validate_quantity(" 12.5 "), 12.5)
        self.assertEqual(stock.validate_quantity(150), 150.0)
        self.assertEqual(stock.validate_quantity("0.25"), 0.25)

    def test_validate_text_fields(self) -> None:
        self.assertEqual(stock.validate_notes("  delivery  "), "delivery")
        self.assertEqual(stock.validate_notes(None), "")
        self.assertEqual(stock.validate_reference("  PO-42 "), "PO-42")
        self.assertEqual(stock.validate_reference(""), "")
        self.assertEqual(stock.validate_reason("Expired"), "Expired")
        for func in (stock.validate_notes, stock.validate_reference,
                     stock.validate_reason):
            with self.subTest(func=func.__name__):
                with self.assertRaises(ValueError):
                    func("x" * 500)
        with self.assertRaisesRegex(ValueError, "required"):
            stock.validate_reason("   ")

    def test_validate_expiry(self) -> None:
        self.assertIsNone(stock.validate_expiry(""))
        self.assertEqual(stock.validate_expiry("2026-12-31"), "2026-12-31")
        for bad in ("31-12-2026", "2026-02-30", "2026/12/31"):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    stock.validate_expiry(bad)

    def test_transaction_requires_at_least_one_valid_line(self) -> None:
        for bad_lines in ([], None, "not a list"):
            with self.subTest(lines=bad_lines):
                with self.assertRaises(ValueError):
                    self._stock_in(bad_lines)
        with self.assertRaises(ValueError):
            self._stock_in([{"ingredient_id": 9999}])
        self.assertEqual(self._counts(), (0, 0, 0))

    def test_duplicate_ingredient_in_one_transaction_is_rejected(self) -> None:
        line = self._line("Pork", "100")
        with self.assertRaisesRegex(ValueError, "only once"):
            self._stock_in([line, dict(line)])
        self.assertEqual(self._counts(), (0, 0, 0))
        self.assertEqual(self._balance("Pork"), 0)

    # ------------------------------------------------------------------
    # Stock-in
    # ------------------------------------------------------------------
    def test_stock_in_increases_balance_and_writes_header_detail_and_view(self) -> None:
        result = self._stock_in(
            [self._line("Ramen noodles", "3000")],
            reference_no="PO-1042",
            notes="Weekly delivery",
        )
        self.assertEqual(result["line_count"], 1)
        self.assertEqual(self._balance("Ramen noodles"), 3000)
        self.assertEqual(self._counts(), (1, 0, 1))
        header = self._raw(
            "SELECT reference_no, notes, created_by, transaction_date "
            "FROM stock_in_transactions"
        )[0]
        self.assertEqual(header[0], "PO-1042")
        self.assertEqual(header[1], "Weekly delivery")
        self.assertEqual(header[2], 1)
        self.assertTrue(header[3])
        detail = self._raw(
            "SELECT ingredient_id, quantity, unit, expiration_date "
            "FROM stock_in_details"
        )[0]
        self.assertEqual(detail[0], self._id("Ramen noodles"))
        self.assertEqual(detail[1], 3000)
        self.assertEqual(detail[2], "grams")  # unit comes from the catalog
        self.assertIsNone(detail[3])
        view = stock.get_history(db_path=self.db_path)["rows"][0]
        self.assertEqual(view["movement_type"], "IN")
        self.assertEqual(view["quantity"], 3000)
        self.assertEqual(view["ingredient_name"], "Ramen noodles")

    def test_multi_line_stock_in_is_one_transaction(self) -> None:
        result = self._stock_in(
            [self._line("Pork", "2000"), self._line("Egg", "20")]
        )
        self.assertEqual(result["line_count"], 2)
        self.assertEqual(self._counts(), (1, 0, 2))
        self.assertEqual(
            self._raw("SELECT COUNT(*) FROM stock_in_details")[0][0], 2
        )
        self.assertEqual(
            {row[0] for row in self._raw(
                "SELECT stock_in_id FROM stock_in_details"
            )},
            {result["transaction_id"]},
        )
        self.assertEqual(self._balance("Pork"), 2000)
        self.assertEqual(self._balance("Egg"), 20)

    def test_stock_in_expiry_updates_ingredient_to_earliest_date(self) -> None:
        self._stock_in(
            [self._line("Pork", "100", expiration_date="2026-12-31")]
        )
        self.assertEqual(
            self._raw(
                "SELECT expiration_date FROM ingredients WHERE name = 'Pork'"
            )[0][0],
            "2026-12-31",
        )
        self._stock_in(
            [self._line("Pork", "100", expiration_date="2026-11-20")]
        )
        self.assertEqual(
            self._raw(
                "SELECT expiration_date FROM ingredients WHERE name = 'Pork'"
            )[0][0],
            "2026-11-20",
        )
        self._stock_in(
            [self._line("Pork", "100", expiration_date="2027-01-01")]
        )
        self.assertEqual(
            self._raw(
                "SELECT expiration_date FROM ingredients WHERE name = 'Pork'"
            )[0][0],
            "2026-11-20",
        )
        self.assertEqual(self._balance("Pork"), 300)

    def test_stock_in_rejects_inactive_or_unknown_ingredient(self) -> None:
        new_id = repository.add_ingredient(
            "Bamboo shoots", "grams", 5, "", self.db_path
        )
        repository.delete_ingredient(new_id, self.db_path)
        for ingredient_id in (9999, new_id):
            with self.subTest(ingredient_id=ingredient_id):
                with self.assertRaisesRegex(ValueError, "no longer active"):
                    self._stock_in(
                        [{"ingredient_id": ingredient_id, "quantity": "10"}]
                    )
        self.assertEqual(self._counts(), (0, 0, 0))

    def test_stock_in_rejects_bad_line_before_any_write(self) -> None:
        for quantity in ("", "0", "-5", "abc"):
            with self.subTest(quantity=quantity):
                with self.assertRaises(ValueError):
                    self._stock_in([self._line("Egg", quantity)])
        with self.assertRaises(ValueError):
            self._stock_in(
                [self._line("Egg", "5", expiration_date="2026-02-30")]
            )
        with self.assertRaises(ValueError):
            self._stock_in([self._line("Egg", "5")], notes="x" * 300)
        self.assertEqual(self._counts(), (0, 0, 0))
        self.assertEqual(self._balance("Egg"), 0)

    # ------------------------------------------------------------------
    # Stock-out
    # ------------------------------------------------------------------
    def test_stock_out_decreases_balance_and_records_reason(self) -> None:
        self._stock_in([self._line("Pork", "1000")])
        result = self._stock_out(
            [self._line("Pork", "250")], "Consumption", notes="Lunch service"
        )
        self.assertEqual(result["line_count"], 1)
        self.assertEqual(self._balance("Pork"), 750)
        self.assertEqual(self._counts(), (1, 1, 2))
        header = self._raw(
            "SELECT reason, notes, created_by FROM stock_out_transactions"
        )[0]
        self.assertEqual(header[0], "Consumption")
        self.assertEqual(header[1], "Lunch service")
        self.assertEqual(header[2], 1)
        detail = self._raw(
            "SELECT quantity, unit FROM stock_out_details"
        )[0]
        self.assertEqual(detail, (250, "grams"))
        out_rows = stock.get_history("OUT", db_path=self.db_path)["rows"]
        self.assertEqual(len(out_rows), 1)
        self.assertEqual(out_rows[0]["quantity"], -250)
        self.assertEqual(out_rows[0]["reason"], "Consumption")

    def test_stock_out_refuses_negative_balance_without_any_write(self) -> None:
        self._stock_in([self._line("Pork", "100")])
        before = self._counts()
        with self.assertRaisesRegex(ValueError, "Not enough Pork"):
            self._stock_out([self._line("Pork", "101")])
        self.assertEqual(self._counts(), before)
        self.assertEqual(self._balance("Pork"), 100)
        self.assertEqual(
            self._raw("SELECT COUNT(*) FROM stock_out_details")[0][0], 0
        )

    def test_multi_line_stock_out_is_atomic_when_one_line_is_short(self) -> None:
        self._stock_in(
            [self._line("Pork", "1000"), self._line("Egg", "2")]
        )
        before = self._counts()
        with self.assertRaisesRegex(ValueError, "Not enough Egg"):
            self._stock_out(
                [self._line("Pork", "100"), self._line("Egg", "3")]
            )
        self.assertEqual(self._counts(), before)
        self.assertEqual(self._balance("Pork"), 1000)
        self.assertEqual(self._balance("Egg"), 2)
        self.assertEqual(
            self._raw("SELECT COUNT(*) FROM stock_out_details")[0][0], 0
        )

    def test_stock_out_can_empty_a_balance_but_never_go_below_zero(self) -> None:
        self._stock_in([self._line("Egg", "2")])
        self._stock_out([self._line("Egg", "2")])
        self.assertEqual(self._balance("Egg"), 0)
        with self.assertRaisesRegex(ValueError, "Not enough"):
            self._stock_out([self._line("Egg", "0.001")])
        self.assertEqual(self._balance("Egg"), 0)
        self.assertEqual(self._raw("PRAGMA integrity_check")[0][0], "ok")

    def test_stock_out_requires_a_reason(self) -> None:
        self._stock_in([self._line("Egg", "5")])
        for bad in ("", "   ", None):
            with self.subTest(reason=bad):
                with self.assertRaisesRegex(ValueError, "reason"):
                    self._stock_out([self._line("Egg", "1")], bad)
        self.assertEqual(self._counts(), (1, 0, 1))

    def test_schema_check_also_blocks_a_direct_negative_balance(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            with database.transaction(self.db_path) as conn:
                conn.execute(
                    "UPDATE ingredients SET quantity_on_hand = -1 "
                    "WHERE name = 'Pork'"
                )
        self.assertEqual(self._balance("Pork"), 0)

    def test_invalid_user_id_is_not_recorded_as_created_by(self) -> None:
        result = self._stock_in(
            [self._line("Egg", "3")], user_id=9999
        )
        self.assertIsNone(
            self._raw(
                "SELECT created_by FROM stock_in_transactions "
                "WHERE stock_in_id = ?",
                (result["transaction_id"],),
            )[0][0]
        )

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------
    def _mixed_history(self) -> None:
        self._stock_in(
            [self._line("Pork", "1000"), self._line("Egg", "20")],
            reference_no="PO-1", notes="Morning delivery",
        )
        self._stock_out(
            [self._line("Pork", "100")], "Expired", notes="Spoiled batch"
        )

    def test_history_totals_and_signed_quantities(self) -> None:
        self._mixed_history()
        data = stock.get_history(db_path=self.db_path)
        self.assertEqual(data["total"], 3)
        self.assertEqual(data["filtered"], 3)
        self.assertEqual(data["stock_in_lines"], 2)
        self.assertEqual(data["stock_out_lines"], 1)
        self.assertNotEqual(data["last_movement"], "—")
        self.assertEqual(
            sorted(row["quantity"] for row in data["rows"]), [-100, 20, 1000]
        )

    def test_history_type_filter(self) -> None:
        self._mixed_history()
        ins = stock.get_history("IN", db_path=self.db_path)
        outs = stock.get_history("OUT", db_path=self.db_path)
        self.assertEqual(ins["filtered"], 2)
        self.assertEqual(outs["filtered"], 1)
        self.assertEqual(
            {row["movement_type"] for row in ins["rows"]}, {"IN"}
        )
        self.assertEqual(
            {row["movement_type"] for row in outs["rows"]}, {"OUT"}
        )
        with self.assertRaises(ValueError):
            stock.get_history("SIDEWAYS", db_path=self.db_path)

    def test_history_search_matches_ingredient_reason_notes_and_date(self) -> None:
        self._mixed_history()
        self.assertEqual(stock.get_history("ALL", "Pork", db_path=self.db_path)["filtered"], 2)
        self.assertEqual(stock.get_history("ALL", "Expired", db_path=self.db_path)["filtered"], 1)
        self.assertEqual(
            stock.get_history("ALL", "Morning delivery", db_path=self.db_path)["filtered"], 2
        )
        self.assertEqual(stock.get_history("ALL", "Spoiled", db_path=self.db_path)["filtered"], 1)
        today = database.local_timestamp()[:10]
        self.assertEqual(stock.get_history("ALL", today, db_path=self.db_path)["filtered"], 3)
        self.assertEqual(stock.get_history("ALL", "no-such-value", db_path=self.db_path)["filtered"], 0)
        # LIKE wildcards in a search string are treated literally.
        self.assertEqual(stock.get_history("ALL", "%", db_path=self.db_path)["filtered"], 0)

    def test_history_limit_caps_rows_but_not_counts(self) -> None:
        self._mixed_history()
        data = stock.get_history("ALL", "", 2, self.db_path)
        self.assertEqual(len(data["rows"]), 2)
        self.assertEqual(data["filtered"], 3)
        self.assertEqual(data["total"], 3)
        self.assertEqual(data["limit"], 2)
        self.assertEqual(len(stock.get_recent_movements(1, self.db_path)), 1)

    def test_movement_detail_for_stock_in(self) -> None:
        self._mixed_history()
        row = self._raw(
            "SELECT stock_in_id, stock_in_detail_id FROM stock_in_details "
            "ORDER BY stock_in_detail_id LIMIT 1"
        )[0]
        detail = stock.get_movement_detail("IN", row[0], row[1], self.db_path)
        self.assertEqual(detail["movement_type"], "IN")
        self.assertEqual(detail["reference_no"], "PO-1")
        self.assertEqual(detail["created_by"], "admin")
        self.assertEqual(detail["ingredient_name"], "Pork")
        self.assertEqual(detail["quantity"], 1000)
        self.assertEqual(detail["unit"], "grams")
        self.assertEqual(detail["notes"], "Morning delivery")
        self.assertIsNone(detail["reason"])

    def test_movement_detail_for_stock_out(self) -> None:
        self._mixed_history()
        row = self._raw(
            "SELECT stock_out_id, stock_out_detail_id FROM stock_out_details"
        )[0]
        detail = stock.get_movement_detail("OUT", row[0], row[1], self.db_path)
        self.assertEqual(detail["movement_type"], "OUT")
        self.assertEqual(detail["reason"], "Expired")
        self.assertEqual(detail["quantity"], -100)
        self.assertEqual(detail["notes"], "Spoiled batch")
        self.assertIsNone(detail["reference_no"])

    def test_missing_or_unknown_movement_detail_raises(self) -> None:
        with self.assertRaises(ValueError):
            stock.get_movement_detail("IN", 9999, 1, self.db_path)
        with self.assertRaises(ValueError):
            stock.get_movement_detail("UNKNOWN", 1, 1, self.db_path)

    # ------------------------------------------------------------------
    # Cross-stage guarantees
    # ------------------------------------------------------------------
    def test_stock_movements_never_change_the_recipe(self) -> None:
        with database.transaction(self.db_path) as conn:
            conn.execute("UPDATE recipes SET updated_at = '2000-01-01 00:00:00'")
        before = {
            line["name"]: line["quantity_per_serving"]
            for line in rr.list_recipe_lines(self.db_path)
        }
        self._mixed_history()
        after = {
            line["name"]: line["quantity_per_serving"]
            for line in rr.list_recipe_lines(self.db_path)
        }
        self.assertEqual(before, after)
        self.assertEqual(
            rr.get_recipe(self.db_path)["updated_at"], "2000-01-01 00:00:00"
        )

    def test_recipe_edits_still_create_no_movements(self) -> None:
        rr.update_quantity(
            rr.list_recipe_lines(self.db_path)[0]["recipe_ingredient_id"],
            "175", self.db_path,
        )
        rr.update_notes("Stage 5 check", self.db_path)
        self.assertEqual(self._counts(), (0, 0, 0))

    def test_ingredient_edit_does_not_change_balance(self) -> None:
        self._stock_in([self._line("Pork", "500")])
        repository.update_ingredient(
            self._id("Pork"), "Pork shoulder", "grams", 50, "", self.db_path
        )
        self.assertEqual(
            self._raw(
                "SELECT quantity_on_hand FROM ingredients WHERE name = 'Pork shoulder'"
            )[0][0],
            500,
        )

    def test_dashboard_snapshot_and_summary_reflect_new_balances(self) -> None:
        self._stock_in(
            [self._line("Ramen noodles", "3000"), self._line("Egg", "2")]
        )
        dashboard = database.get_dashboard_data(self.db_path)
        self.assertEqual(dashboard["out_of_stock"], 6)
        self.assertEqual(dashboard["low_stock"], 1)  # Egg: 2 <= minimum 2
        self.assertEqual(len(dashboard["recent_activities"]), 2)
        self.assertEqual(
            database.get_system_snapshot(self.db_path)["stock_movements"], 2
        )
        summary = stock.get_inventory_summary(self.db_path)
        self.assertEqual(summary["with_stock"], 2)
        self.assertEqual(summary["total_movements"], 2)
        self.assertEqual(summary["last_movement"],
                         dashboard["recent_activities"][0]["transaction_date"])

    def test_soft_deleted_ingredient_keeps_its_history_valid(self) -> None:
        self._stock_in([self._line("Garlic", "50")])
        repository.delete_ingredient(self._id("Garlic"), self.db_path)
        self.assertEqual(
            stock.get_history(db_path=self.db_path)["filtered"], 1
        )
        self.assertEqual(
            self._raw("SELECT COUNT(*) FROM stock_in_details")[0][0], 1
        )
        self.assertEqual(self._raw("PRAGMA foreign_key_check"), [])

    def test_integrity_and_foreign_keys_after_all_operations(self) -> None:
        self._mixed_history()
        self._stock_in([self._line("Garlic", "25", expiration_date="2026-12-01")])
        self._stock_out([self._line("Egg", "1")], "Manual correction")
        self.assertEqual(self._raw("PRAGMA integrity_check")[0][0], "ok")
        self.assertEqual(self._raw("PRAGMA foreign_key_check"), [])
        self.assertEqual(self._counts()[2], 5)
        self.assertTrue(
            all(balance >= 0 for (balance,) in self._raw(
                "SELECT quantity_on_hand FROM ingredients"
            ))
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
