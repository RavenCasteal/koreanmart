"""
Stage 9 automated checks - Point of Sale, receipts and legacy migration.

Run from the project folder:
    python -m unittest discover -s tests -v

What these tests protect:
  * the idempotent, non-destructive legacy migration (no re-seed,
    no admin-password reset, no data loss). Stage 11a raises user_version
    to 4, so migration assertions use database.SCHEMA_VERSION.
  * Philippine peso (PHP / the peso sign) as the ONLY money format
  * preview / read helpers are strictly READ-ONLY (zero bytes, zero rows)
  * a confirmed sale is ONE atomic transaction: sale header + deduction
    snapshots + ordinary stock-out + balance updates, all or nothing
  * insufficient stock, short payment and mid-transaction failures create
    ZERO writes; the existing Calculator remains read-only
  * void = immutable history + compensating stock-in using ORIGINAL snapshots
  * every receipt contains the required fields and is always archived,
    and a printer failure can never undo a committed sale
  * the existing Dashboard / Inventory History see POS deductions via
    vw_stock_movements without any query changes

Every test uses a temporary SQLite database. The real
database/ramen_inventory.db is only ever COPIED (never written) and is also
opened read-only for an integrity check at the end. No GUI is opened here.
"""

from __future__ import annotations

import ast
import hashlib
import re
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any
from unittest.mock import patch

import database
import formatting
import receipt
import sales_repository as sales
import stock_repository as stock
from components.shell import PAGES

ROOT = Path(__file__).resolve().parents[1]
REAL_DB = ROOT / "database" / "ramen_inventory.db"

ALL_TABLES = {
    "users", "menu_items", "recipes", "recipe_ingredients", "ingredients",
    "stock_in_transactions", "stock_in_details",
    "stock_out_transactions", "stock_out_details",
    "sales_transactions", "sale_deductions",
}
# Stage 12 adds these tables on first launch via products.ensure_schema().
# The delivered real DB may already contain them, so a copy of it is allowed
# to have exactly these extras on top of the v4 core set.
STAGE12_TABLES = {"app_meta", "store_sales", "store_sale_items"}
MOVEMENT_TABLES = (
    "sales_transactions", "sale_deductions",
    "stock_out_transactions", "stock_out_details",
    "stock_in_transactions", "stock_in_details",
)
# Deliberately phrase-based: a bare literal such as "replace" (used by
# str.encode in receipt.py) must NOT be mistaken for SQL REPLACE INTO.
_SQL_WRITE = re.compile(
    r"^(?:insert\s+into|update\s+\w+\s+set|delete\s+from|"
    r"replace\s+into|create\s+(?:table|view|index|trigger)|"
    r"drop\s+(?:table|view|index|trigger)|alter\s+table)\b",
    re.IGNORECASE,
)


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _write_sql_literals(relative: str) -> list[str]:
    """Find string literals that are actually SQL writes (AST, not prose)."""
    tree = ast.parse(_source(relative))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            literal = node.value.strip()
            if _SQL_WRITE.match(literal):
                found.append(literal[:60])
    return found


class _TempDB(unittest.TestCase):
    """Shared temp-database helpers. Contains no test methods itself."""

    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.db_path = Path(temp_dir.name) / "ramen_inventory.db"
        database.initialize_database(self.db_path)

    # -- helpers ------------------------------------------------------
    def _hash(self) -> str:
        return hashlib.sha256(self.db_path.read_bytes()).hexdigest()

    def _stock(self, servings: int) -> None:
        """Put exactly N servings' worth of every ingredient on hand."""
        with database.transaction(self.db_path) as conn:
            rows = conn.execute(
                "SELECT ingredient_id, quantity_per_serving "
                "FROM recipe_ingredients"
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE ingredients SET quantity_on_hand = ? "
                    "WHERE ingredient_id = ?",
                    (
                        row["quantity_per_serving"] * servings,
                        row["ingredient_id"],
                    ),
                )

    def _balances(self) -> dict[str, float]:
        with closing(database.get_connection(self.db_path)) as conn:
            return {
                row["name"]: float(row["quantity_on_hand"])
                for row in conn.execute(
                    "SELECT name, quantity_on_hand FROM ingredients"
                )
            }

    def _counts(self) -> dict[str, int]:
        with closing(database.get_connection(self.db_path)) as conn:
            return {
                name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                for name in MOVEMENT_TABLES
            }

    def _sale(
        self,
        servings: int = 1,
        method: str = "Cash",
        tendered: Any = None,
        user_id: Any = None,
    ) -> dict[str, Any]:
        if tendered is None:
            tendered = sales.get_unit_price(self.db_path) * servings
        return sales.confirm_sale(
            servings, method, tendered,
            user_id=user_id, db_path=self.db_path,
        )

    def _preview(
        self, servings: Any = 1, method: str = "Cash", tendered: Any = None
    ) -> dict[str, Any]:
        if tendered is None:
            try:
                tendered = sales.get_unit_price(self.db_path) * float(servings)
            except (TypeError, ValueError):
                # Bad servings must still reach preview_sale so IT raises.
                tendered = 100_000_000
        return sales.preview_sale(
            servings, method, tendered, db_path=self.db_path
        )

    def _read_only_connection(self, _path: Any) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn


# ======================================================================
# 1 - Schema, currency and migration
# ======================================================================
class Stage9SchemaTests(_TempDB):

    def test_all_eleven_tables_and_the_view_exist(self) -> None:
        with closing(database.get_connection(self.db_path)) as conn:
            tables = {
                row["name"] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            }
            views = {
                row["name"] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'view'"
                )
            }
            self.assertEqual(tables, ALL_TABLES)
            self.assertIn("vw_stock_movements", views)

    def test_schema_version_matches_the_current_build(self) -> None:
        with closing(database.get_connection(self.db_path)) as conn:
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0],
                database.SCHEMA_VERSION,
            )
        self.assertEqual(database.SCHEMA_VERSION, 4)

    def test_unit_price_column_has_the_documented_php_default(self) -> None:
        with closing(database.get_connection(self.db_path)) as conn:
            columns = {
                row["name"]: row for row in conn.execute(
                    "PRAGMA table_info(menu_items)"
                )
            }
            self.assertIn("unit_price", columns)
            self.assertEqual(columns["unit_price"]["dflt_value"], "150.0")
            self.assertEqual(
                conn.execute(
                    "SELECT unit_price FROM menu_items"
                ).fetchone()[0], 150.0,
            )
        self.assertEqual(sales.get_unit_price(self.db_path), 150.0)
        self.assertEqual(database.DEFAULT_UNIT_PRICE, 150.0)

    def test_integrity_and_foreign_keys_are_clean(self) -> None:
        with closing(database.get_connection(self.db_path)) as conn:
            self.assertEqual(
                conn.execute("PRAGMA integrity_check").fetchone()[0], "ok"
            )
            self.assertEqual(list(conn.execute("PRAGMA foreign_key_check")), [])
            sale_fks = {
                row["table"] for row in conn.execute(
                    "PRAGMA foreign_key_list(sales_transactions)"
                )
            }
            self.assertIn("stock_out_transactions", sale_fks)
            self.assertIn("users", sale_fks)
            deduction_fks = {
                row["table"] for row in conn.execute(
                    "PRAGMA foreign_key_list(sale_deductions)"
                )
            }
            self.assertIn("sales_transactions", deduction_fks)
            self.assertIn("ingredients", deduction_fks)

    def test_sale_constraints_reject_bad_status_and_nonpositive_amounts(self) -> None:
        schema = _source("database/schema.sql")
        self.assertIn("CHECK (status IN ('completed', 'voided'))", schema)
        self.assertIn("CHECK (quantity > 0)", schema)
        self.assertIn("CHECK (total_amount >= 0)", schema)
        self.assertIn("CHECK (balance_after >= 0)", schema)
        self.assertIn("DEFAULT 'PHP'", schema)
        self.assertIn("transaction_number  TEXT    NOT NULL UNIQUE", schema)

    def test_initializing_twice_never_reseeds_or_resets_a_password(self) -> None:
        with database.transaction(self.db_path) as conn:
            conn.execute("UPDATE users SET password_hash = 'SENTINEL-HASH'")
            conn.execute(
                "UPDATE ingredients SET quantity_on_hand = 432 "
                "WHERE name = 'Pork'"
            )
        database.initialize_database(self.db_path)
        with closing(database.get_connection(self.db_path)) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1
            )
            self.assertEqual(
                conn.execute("SELECT password_hash FROM users").fetchone()[0],
                "SENTINEL-HASH",
            )
            self.assertEqual(
                conn.execute(
                    "SELECT quantity_on_hand FROM ingredients WHERE name = 'Pork'"
                ).fetchone()[0], 432,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM ingredients").fetchone()[0], 8
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM recipe_ingredients")
                .fetchone()[0], 8
            )

    def test_legacy_version_one_database_migrates_without_data_loss(self) -> None:
        """Simulate the delivered pre-Stage-9 DB, then run the migration."""
        conn = database.get_connection(self.db_path)
        try:
            conn.execute("DROP TABLE sale_deductions")
            conn.execute("DROP TABLE sales_transactions")
            conn.execute("ALTER TABLE menu_items DROP COLUMN unit_price")
            conn.execute("PRAGMA user_version = 1")
            conn.execute("UPDATE users SET password_hash = 'LEGACY-HASH'")
            conn.execute(
                "UPDATE ingredients SET quantity_on_hand = 777 "
                "WHERE name = 'Ramen broth'"
            )
            conn.commit()
        finally:
            conn.close()

        database.initialize_database(self.db_path)

        with closing(database.get_connection(self.db_path)) as conn:
            tables = {
                row["name"] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            }
            self.assertEqual(tables, ALL_TABLES)
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0],
                database.SCHEMA_VERSION,
            )
            self.assertIn(
                "unit_price",
                {row["name"] for row in conn.execute(
                    "PRAGMA table_info(menu_items)"
                )},
            )
            self.assertEqual(
                conn.execute("SELECT unit_price FROM menu_items").fetchone()[0],
                150.0,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1
            )
            self.assertEqual(
                conn.execute("SELECT password_hash FROM users").fetchone()[0],
                "LEGACY-HASH",
            )
            self.assertEqual(
                conn.execute(
                    "SELECT quantity_on_hand FROM ingredients "
                    "WHERE name = 'Ramen broth'"
                ).fetchone()[0], 777,
            )
            self.assertEqual(
                conn.execute("PRAGMA integrity_check").fetchone()[0], "ok"
            )
            self.assertEqual(list(conn.execute("PRAGMA foreign_key_check")), [])
        self.assertEqual(sales.get_unit_price(self.db_path), 150.0)

    def test_migration_is_idempotent_on_a_legacy_database(self) -> None:
        conn = database.get_connection(self.db_path)
        try:
            conn.execute("DROP TABLE sale_deductions")
            conn.execute("DROP TABLE sales_transactions")
            conn.execute("ALTER TABLE menu_items DROP COLUMN unit_price")
            conn.execute("PRAGMA user_version = 1")
            conn.commit()
        finally:
            conn.close()
        database.initialize_database(self.db_path)
        first = self._hash()
        database.initialize_database(self.db_path)
        self.assertEqual(self._hash(), first)
        self.assertEqual(
            sales.get_sales_history(db_path=self.db_path)["summary"]["total_sales"],
            0,
        )

    def test_sale_is_reserved_and_not_a_manual_stock_out_reason(self) -> None:
        self.assertNotIn("Sale", stock.STOCK_OUT_REASONS)
        self.assertEqual(sales.SALE_REASON, "Sale")
        self.assertIn("Sale", _source("database/schema.sql"))


class Stage9MoneyTests(unittest.TestCase):

    def test_currency_is_php_with_the_peso_sign(self) -> None:
        self.assertEqual(formatting.CURRENCY_CODE, "PHP")
        self.assertEqual(formatting.CURRENCY_SYMBOL, "\u20b1")
        self.assertIs(formatting.format_peso, formatting.format_money)

    def test_format_money_always_uses_two_decimals_and_the_peso_sign(self) -> None:
        self.assertEqual(formatting.format_money(150), "\u20b1150.00")
        self.assertEqual(formatting.format_money(1250.5), "\u20b11,250.50")
        self.assertEqual(formatting.format_money(0), "\u20b10.00")
        self.assertEqual(formatting.format_money(None), "\u20b10.00")
        self.assertEqual(formatting.format_money("bad"), "\u20b10.00")
        self.assertEqual(formatting.format_money(-12.345), "\u20b1-12.35")

    def test_validation_messages_use_the_shared_peso_formatter(self) -> None:
        self.assertEqual(sales.format_peso_short(150), "\u20b1150.00")

    def test_no_yen_or_yuan_symbols_anywhere_in_application_source(self) -> None:
        offenders: list[str] = []
        for pattern in ("*.py", "*.sql", "*.txt", "*.bat"):
            for path in ROOT.rglob(pattern):
                if any(part in {".venv", "__pycache__", "receipts"}
                       for part in path.parts):
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
                if "\u00a5" in text or "\uffe5" in text:
                    offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [])

    def test_pos_and_receipt_use_the_one_shared_money_formatter(self) -> None:
        self.assertIn("format_money", _source("pages/pos.py"))
        self.assertIn("format_money", _source("receipt.py"))
        self.assertIn("from formatting import", _source("pages/pos.py"))
        self.assertIn("from formatting import", _source("receipt.py"))


# ======================================================================
# 2 - Validation and strict read-only guarantees
# ======================================================================
class Stage9ValidationTests(_TempDB):

    def test_payment_methods(self) -> None:
        for method in sales.PAYMENT_METHODS:
            self.assertEqual(sales.validate_payment_method(method), method)
        self.assertEqual(sales.validate_payment_method("  Cash  "), "Cash")
        for bad in ("", "BITCOIN", None, 12):
            with self.assertRaises(ValueError):
                sales.validate_payment_method(bad)

    def test_amount_accepts_peso_sign_and_thousands_separator(self) -> None:
        self.assertEqual(sales.validate_amount("\u20b11,250.50"), 1250.5)
        self.assertEqual(sales.validate_amount(" 500 "), 500.0)
        self.assertEqual(sales.validate_amount(0), 0.0)

    def test_amount_rejects_bad_values(self) -> None:
        for bad in ("", "abc", "-1", "NaN", "Infinity", "1e309",
                    1_000_000_001):
            with self.assertRaises(ValueError):
                sales.validate_amount(bad)

    def test_unit_price_validation(self) -> None:
        self.assertEqual(sales.validate_unit_price("175.25"), 175.25)
        for bad in ("0", "-5", "abc", 1_000_001):
            with self.assertRaises(ValueError):
                sales.validate_unit_price(bad)

    def test_void_reason_validation(self) -> None:
        self.assertEqual(sales.validate_void_reason("  wrong item  "),
                         "wrong item")
        with self.assertRaises(ValueError):
            sales.validate_void_reason("   ")
        with self.assertRaises(ValueError):
            sales.validate_void_reason("x" * 201)

    def test_invalid_servings_are_rejected_by_preview_and_confirm(self) -> None:
        self._stock(5)
        for bad in ("", "0", "-1", "1.5", "abc", "10001"):
            with self.assertRaises(ValueError, msg=str(bad)):
                self._preview(bad)
            with self.assertRaises(ValueError, msg=str(bad)):
                sales.confirm_sale(
                    bad, "Cash", 100_000_000, db_path=self.db_path
                )
        self.assertEqual(
            sales.get_sales_history(db_path=self.db_path)["summary"]["total_sales"],
            0,
        )

    def test_preview_rejects_amount_tendered_below_total(self) -> None:
        self._stock(3)
        with self.assertRaises(ValueError) as caught:
            self._preview(2, tendered=100)
        self.assertIn("less than the total", str(caught.exception))
        self.assertIn("\u20b1", str(caught.exception))

    def test_preview_rejects_an_empty_recipe(self) -> None:
        with database.transaction(self.db_path) as conn:
            conn.execute("DELETE FROM recipe_ingredients")
        with self.assertRaises(ValueError) as caught:
            self._preview(1)
        self.assertIn("no ingredients", str(caught.exception))

    def test_confirm_rejects_an_empty_recipe(self) -> None:
        with database.transaction(self.db_path) as conn:
            conn.execute("DELETE FROM recipe_ingredients")
        with self.assertRaises(ValueError):
            self._sale(1)


class Stage9ReadOnlyTests(_TempDB):

    def test_preview_writes_zero_bytes(self) -> None:
        self._stock(5)
        before = self._hash()
        for _ in range(3):
            self._preview(2)
        self.assertEqual(self._hash(), before)

    def test_preview_creates_no_rows_anywhere(self) -> None:
        self._stock(5)
        before = self._counts()
        self._preview(2)
        self.assertEqual(self._counts(), before)
        self.assertTrue(all(value == 0 for value in before.values()))

    def test_all_read_helpers_write_zero_bytes(self) -> None:
        self._stock(4)
        before = self._hash()
        sales.get_unit_price(self.db_path)
        sales.get_pos_context(self.db_path)
        sales.get_sales_history(db_path=self.db_path)
        sales.get_sales_history("COMPLETED", "abc", db_path=self.db_path)
        with self.assertRaises(ValueError):
            sales.get_sale_detail(999, db_path=self.db_path)
        database.get_dashboard_data(self.db_path)
        self.assertEqual(self._hash(), before)

    def test_preview_works_on_a_read_only_sqlite_connection(self) -> None:
        self._stock(3)
        with patch("database.get_connection",
                   side_effect=self._read_only_connection):
            result = self._preview(2)
        self.assertTrue(result["sufficient"])
        self.assertEqual(result["total_amount"], 300.0)

    def test_pos_context_works_on_a_read_only_sqlite_connection(self) -> None:
        self._stock(3)
        with patch("database.get_connection",
                   side_effect=self._read_only_connection):
            context = sales.get_pos_context(self.db_path)
        self.assertEqual(context["max_servings"], 3)
        self.assertEqual(context["currency_code"], "PHP")

    def test_calculator_layers_are_still_write_free(self) -> None:
        self.assertEqual(_write_sql_literals("usage_repository.py"), [])
        self.assertEqual(_write_sql_literals("pages/calculator.py"), [])
        self.assertNotIn("sales_repository", _source("pages/calculator.py"))
        self.assertNotIn("receipt", _source("pages/calculator.py"))

    def test_pos_ui_and_receipt_module_contain_no_sql(self) -> None:
        self.assertEqual(_write_sql_literals("pages/pos.py"), [])
        self.assertEqual(_write_sql_literals("receipt.py"), [])
        pos_source = _source("pages/pos.py")
        self.assertIn("sales.confirm_sale(", pos_source)
        self.assertIn("receipt.issue_receipt(", pos_source)

    def test_monitoring_layer_is_still_write_free(self) -> None:
        self.assertEqual(_write_sql_literals("monitoring_repository.py"), [])


# ======================================================================
# 3 - Preview correctness
# ======================================================================
class Stage9PreviewTests(_TempDB):

    def test_required_equals_recipe_quantity_times_servings(self) -> None:
        self._stock(5)
        result = self._preview(3)
        self.assertEqual(len(result["lines"]), 8)
        for line in result["lines"]:
            self.assertAlmostEqual(
                line["quantity_required"],
                line["quantity_per_serving"] * 3,
                places=6,
            )
        noodles = next(
            line for line in result["lines"]
            if line["name"] == "Ramen noodles"
        )
        self.assertEqual(noodles["quantity_required"], 450.0)
        self.assertEqual(noodles["unit"], "grams")

    def test_preview_is_sufficient_at_the_exact_boundary(self) -> None:
        self._stock(3)
        result = self._preview(3)
        self.assertTrue(result["sufficient"])
        self.assertEqual(result["shortages"], [])
        self.assertTrue(all(line["sufficient"] for line in result["lines"]))
        self.assertEqual(result["max_servings"], 3)

    def test_preview_reports_shortages_and_maximum_servings(self) -> None:
        self._stock(2)
        result = self._preview(5)
        self.assertFalse(result["sufficient"])
        self.assertEqual(len(result["shortages"]), 8)
        self.assertEqual(result["max_servings"], 2)
        noodles = next(
            line for line in result["shortages"]
            if line["name"] == "Ramen noodles"
        )
        self.assertEqual(noodles["shortfall"], 450.0)

    def test_preview_payment_totals_and_change(self) -> None:
        self._stock(5)
        result = self._preview(2, "Card", 500)
        self.assertEqual(result["unit_price"], 150.0)
        self.assertEqual(result["total_amount"], 300.0)
        self.assertEqual(result["change_amount"], 200.0)
        self.assertEqual(result["payment_method"], "Card")
        self.assertEqual(result["currency_code"], "PHP")

    def test_pos_context_summary_starts_empty(self) -> None:
        context = sales.get_pos_context(self.db_path)
        summary = context["summary"]
        self.assertEqual(summary["total_sales"], 0)
        self.assertEqual(summary["completed_sales"], 0)
        self.assertEqual(summary["voided_sales"], 0)
        self.assertEqual(summary["gross_sales"], 0.0)
        self.assertEqual(context["max_servings"], 0)


# ======================================================================
# 4 - Confirm: one atomic transaction
# ======================================================================
class Stage9ConfirmTests(_TempDB):

    def setUp(self) -> None:
        super().setUp()
        self._stock(5)

    def test_confirm_creates_sale_deductions_and_stock_out_rows(self) -> None:
        result = self._sale(2, "Cash", 500)
        counts = self._counts()
        self.assertEqual(counts["sales_transactions"], 1)
        self.assertEqual(counts["sale_deductions"], 8)
        self.assertEqual(counts["stock_out_transactions"], 1)
        self.assertEqual(counts["stock_out_details"], 8)
        self.assertEqual(counts["stock_in_transactions"], 0)
        self.assertEqual(len(result["lines"]), 8)

        with closing(database.get_connection(self.db_path)) as conn:
            header = conn.execute(
                "SELECT reason, notes FROM stock_out_transactions"
            ).fetchone()
            self.assertEqual(header["reason"], "Sale")
            self.assertIn(result["transaction_number"], header["notes"])

    def test_confirm_deducts_exact_recipe_quantities(self) -> None:
        before = self._balances()
        result = self._sale(2)
        after = self._balances()
        for name, quantity in before.items():
            line = next(
                item for item in result["lines"] if item["name"] == name
            )
            self.assertAlmostEqual(
                after[name], quantity - line["quantity_deducted"], places=6
            )
            self.assertEqual(line["quantity_deducted"],
                             line["quantity_per_serving"] * 2)
        self.assertEqual(after["Ramen noodles"], 450.0)
        self.assertEqual(after["Pork"], 300.0)
        self.assertEqual(after["Ramen broth"], 1200.0)

    def test_confirm_records_immutable_money_snapshots(self) -> None:
        result = self._sale(2, "Cash", 500)
        detail = sales.get_sale_detail(result["sale_id"], self.db_path)
        self.assertEqual(detail["menu_item_name"], "Regular Ramen")
        self.assertEqual(detail["quantity"], 2)
        self.assertEqual(detail["unit_price"], 150.0)
        self.assertEqual(detail["total_amount"], 300.0)
        self.assertEqual(detail["payment_method"], "Cash")
        self.assertEqual(detail["amount_tendered"], 500.0)
        self.assertEqual(detail["change_amount"], 200.0)
        self.assertEqual(detail["currency_code"], "PHP")
        self.assertEqual(detail["status"], "completed")
        self.assertRegex(detail["transaction_date"],
                         r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_transaction_number_format_and_sequence(self) -> None:
        first = self._sale(1)
        second = self._sale(1)
        self.assertRegex(first["transaction_number"],
                         r"^RR-\d{8}-0001$")
        self.assertRegex(second["transaction_number"],
                         r"^RR-\d{8}-0002$")
        self.assertNotEqual(
            first["transaction_number"], second["transaction_number"]
        )

    def test_exact_boundary_sale_leaves_zero_balances(self) -> None:
        self._stock(2)
        result = self._sale(2)
        self.assertTrue(result["lines"])
        self.assertTrue(all(value == 0 for value in self._balances().values()))

    def test_confirm_over_available_stock_raises_and_writes_nothing(self) -> None:
        self._stock(2)
        before_counts, before_balances, before_hash = (
            self._counts(), self._balances(), self._hash()
        )
        with self.assertRaises(ValueError) as caught:
            self._sale(3)
        self.assertIn("Not enough", str(caught.exception))
        self.assertEqual(self._counts(), before_counts)
        self.assertEqual(self._balances(), before_balances)
        self.assertEqual(self._hash(), before_hash)

    def test_short_tender_and_bad_payment_write_nothing(self) -> None:
        before = self._hash()
        with self.assertRaises(ValueError):
            self._sale(1, "Cash", 10)
        with self.assertRaises(ValueError):
            self._sale(1, "Cheque", 1000)
        self.assertEqual(self._hash(), before)
        self.assertEqual(self._counts()["sales_transactions"], 0)

    def test_inactive_ingredient_blocks_the_whole_sale(self) -> None:
        with database.transaction(self.db_path) as conn:
            conn.execute(
                "UPDATE ingredients SET is_active = 0 WHERE name = 'Garlic'"
            )
        before = self._counts()
        with self.assertRaises(ValueError) as caught:
            self._sale(1)
        self.assertIn("no longer active", str(caught.exception))
        self.assertEqual(self._counts(), before)

    def test_a_failure_after_the_first_insert_rolls_back_everything(self) -> None:
        """Trigger ABORT on sale_deductions - stock-out rows were already
        inserted, so this proves the whole sale is ONE transaction."""
        before_balances = self._balances()
        before_counts = self._counts()
        with database.transaction(self.db_path) as conn:
            conn.execute(
                "CREATE TRIGGER stage9_fail BEFORE INSERT ON sale_deductions "
                "BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self._sale(1)
        self.assertEqual(self._counts(), before_counts)
        self.assertEqual(self._balances(), before_balances)
        with database.transaction(self.db_path) as conn:
            conn.execute("DROP TRIGGER stage9_fail")

    def test_sale_stock_out_is_visible_in_the_unified_movement_view(self) -> None:
        self._sale(2)
        with closing(database.get_connection(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT movement_type, quantity, reason, notes "
                "FROM vw_stock_movements WHERE reason = 'Sale'"
            ).fetchall()
        self.assertEqual(len(rows), 8)
        self.assertTrue(all(row["movement_type"] == "OUT" for row in rows))
        self.assertTrue(all(row["quantity"] < 0 for row in rows))

    def test_existing_history_and_dashboard_see_the_sale_query_free(self) -> None:
        result = self._sale(1)
        history = stock.get_history("OUT", "Sale", db_path=self.db_path)
        self.assertEqual(history["filtered"], 8)
        self.assertTrue(all(
            row["reason"] == "Sale" for row in history["rows"]
        ))
        dashboard = database.get_dashboard_data(self.db_path)
        self.assertTrue(any(
            activity["reason"] == "Sale"
            for activity in dashboard["recent_activities"]
        ))
        self.assertIn(result["transaction_number"],
                      str(dashboard["recent_activities"]))

    def test_price_change_does_not_rewrite_a_past_sale(self) -> None:
        old = self._sale(1, "Cash", 200)
        sales.set_unit_price(200, db_path=self.db_path)
        detail = sales.get_sale_detail(old["sale_id"], self.db_path)
        self.assertEqual(detail["unit_price"], 150.0)
        self.assertEqual(detail["total_amount"], 150.0)
        self.assertEqual(detail["change_amount"], 50.0)
        new = self._sale(1, "Cash", 250)
        self.assertEqual(new["unit_price"], 200.0)
        self.assertEqual(new["total_amount"], 200.0)

    def test_recipe_change_does_not_rewrite_past_deduction_snapshots(self) -> None:
        result = self._sale(1)
        with database.transaction(self.db_path) as conn:
            conn.execute(
                "UPDATE recipe_ingredients SET quantity_per_serving = 999 "
                "WHERE ingredient_id = ("
                "  SELECT ingredient_id FROM ingredients WHERE name = 'Pork'"
                ")"
            )
        detail = sales.get_sale_detail(result["sale_id"], self.db_path)
        pork = next(
            line for line in detail["lines"]
            if line["ingredient_name"] == "Pork"
        )
        self.assertEqual(pork["quantity_per_serving"], 100.0)
        self.assertEqual(pork["quantity_deducted"], 100.0)

    def test_unknown_user_is_recorded_as_no_cashier(self) -> None:
        result = self._sale(1, user_id=999999)
        detail = sales.get_sale_detail(result["sale_id"], self.db_path)
        self.assertEqual(detail["cashier"], "\u2014")
        with closing(database.get_connection(self.db_path)) as conn:
            self.assertIsNone(conn.execute(
                "SELECT created_by FROM sales_transactions"
            ).fetchone()[0])

    def test_real_admin_user_is_recorded_as_cashier(self) -> None:
        with closing(database.get_connection(self.db_path)) as conn:
            user_id = conn.execute(
                "SELECT user_id FROM users"
            ).fetchone()[0]
        result = self._sale(1, user_id=user_id)
        detail = sales.get_sale_detail(result["sale_id"], self.db_path)
        self.assertEqual(detail["cashier"], "admin")

    def test_non_cash_payments_and_multiple_sales_accumulate(self) -> None:
        self._sale(1, "Card", 150)
        self._sale(1, "E-wallet", 150)
        summary = sales.get_sales_history(db_path=self.db_path)["summary"]
        self.assertEqual(summary["total_sales"], 2)
        self.assertEqual(summary["completed_sales"], 2)
        self.assertEqual(summary["gross_sales"], 300.0)
        rows = sales.get_sales_history(db_path=self.db_path)["rows"]
        self.assertEqual({row["payment_method"] for row in rows},
                         {"Card", "E-wallet"})

    def test_sales_history_status_filter_and_search(self) -> None:
        result = self._sale(1)
        number = result["transaction_number"]
        self.assertEqual(
            sales.get_sales_history("COMPLETED", number,
                                    db_path=self.db_path)["filtered"], 1
        )
        self.assertEqual(
            sales.get_sales_history("VOIDED", db_path=self.db_path)["filtered"],
            0,
        )
        self.assertEqual(
            sales.get_sales_history("ALL", "no-such-sale",
                                    db_path=self.db_path)["filtered"], 0
        )
        with self.assertRaises(ValueError):
            sales.get_sales_history("PENDING", db_path=self.db_path)


# ======================================================================
# 5 - Void / cancel after commit
# ======================================================================
class Stage9VoidTests(_TempDB):

    def setUp(self) -> None:
        super().setUp()
        self._stock(5)
        self.result = self._sale(2, "Cash", 500)

    def test_void_marks_status_and_never_deletes_history(self) -> None:
        voided = sales.void_sale(
            self.result["sale_id"], "Customer cancelled",
            db_path=self.db_path,
        )
        self.assertEqual(voided["status"], "voided")
        self.assertEqual(voided["void_reason"], "Customer cancelled")
        detail = sales.get_sale_detail(self.result["sale_id"], self.db_path)
        self.assertEqual(detail["status"], "voided")
        self.assertIsNotNone(detail["voided_at"])
        self.assertEqual(detail["reversal_id"], voided["stock_in_id"])
        counts = self._counts()
        self.assertEqual(counts["sales_transactions"], 1)
        self.assertEqual(counts["sale_deductions"], 8)
        self.assertEqual(counts["stock_out_details"], 8)

    def test_void_restores_balances_with_a_compensating_stock_in(self) -> None:
        original = {
            name: quantity for name, quantity in self._balances().items()
        }
        # Original balance before the sale = current + 2 servings.
        with closing(database.get_connection(self.db_path)) as conn:
            recipe = {
                row["name"]: float(row["quantity_per_serving"])
                for row in conn.execute(
                    "SELECT i.name, ri.quantity_per_serving "
                    "FROM recipe_ingredients ri "
                    "JOIN ingredients i ON i.ingredient_id = ri.ingredient_id"
                )
            }
        expected = {
            name: quantity + recipe[name] * 2
            for name, quantity in original.items()
        }
        sales.void_sale(self.result["sale_id"], "Cancel",
                        db_path=self.db_path)
        self.assertEqual(self._balances(), expected)

        counts = self._counts()
        self.assertEqual(counts["stock_in_transactions"], 1)
        self.assertEqual(counts["stock_in_details"], 8)
        with closing(database.get_connection(self.db_path)) as conn:
            header = conn.execute(
                "SELECT reference_no FROM stock_in_transactions"
            ).fetchone()[0]
            self.assertEqual(
                header, "VOID " + self.result["transaction_number"]
            )

    def test_void_after_recipe_change_uses_original_snapshots(self) -> None:
        before_void = self._balances()
        with database.transaction(self.db_path) as conn:
            conn.execute(
                "UPDATE recipe_ingredients SET quantity_per_serving = 999 "
                "WHERE ingredient_id = ("
                "  SELECT ingredient_id FROM ingredients WHERE name = 'Pork'"
                ")"
            )
        sales.void_sale(self.result["sale_id"], "Wrong order",
                        db_path=self.db_path)
        after = self._balances()
        self.assertEqual(after["Pork"], before_void["Pork"] + 200.0)
        with closing(database.get_connection(self.db_path)) as conn:
            restored = conn.execute(
                "SELECT quantity FROM stock_in_details WHERE ingredient_id = "
                "(SELECT ingredient_id FROM ingredients WHERE name = 'Pork')"
            ).fetchone()[0]
        self.assertEqual(restored, 200.0)

    def test_voided_sale_is_excluded_from_gross_but_kept_in_history(self) -> None:
        sales.void_sale(self.result["sale_id"], "Cancel",
                        db_path=self.db_path)
        summary = sales.get_sales_history(db_path=self.db_path)["summary"]
        self.assertEqual(summary["total_sales"], 1)
        self.assertEqual(summary["completed_sales"], 0)
        self.assertEqual(summary["voided_sales"], 1)
        self.assertEqual(summary["gross_sales"], 0.0)
        self.assertEqual(
            sales.get_sales_history("VOIDED",
                                    db_path=self.db_path)["filtered"], 1
        )

    def test_double_void_is_rejected_with_no_extra_writes(self) -> None:
        sales.void_sale(self.result["sale_id"], "Cancel",
                        db_path=self.db_path)
        counts = self._counts()
        with self.assertRaises(ValueError) as caught:
            sales.void_sale(self.result["sale_id"], "Again",
                            db_path=self.db_path)
        self.assertIn("already voided", str(caught.exception))
        self.assertEqual(self._counts(), counts)

    def test_void_validation_errors_create_no_writes(self) -> None:
        counts = self._counts()
        for reason in ("", "   ", "x" * 201):
            with self.assertRaises(ValueError):
                sales.void_sale(self.result["sale_id"], reason,
                                db_path=self.db_path)
        for bad_id in ("abc", None, 999999):
            with self.assertRaises(ValueError):
                sales.void_sale(bad_id, "Cancel", db_path=self.db_path)
        self.assertEqual(self._counts(), counts)

    def test_void_allows_a_new_sale_afterwards(self) -> None:
        sales.void_sale(self.result["sale_id"], "Cancel",
                        db_path=self.db_path)
        new = self._sale(1, "Cash", 200)
        self.assertEqual(new["status"], "completed")
        self.assertRegex(new["transaction_number"],
                         r"^RR-\d{8}-0002$")


# ======================================================================
# 6 - Receipts and printing
# ======================================================================
SAMPLE_SALE: dict[str, Any] = {
    "transaction_number": "RR-20261110-0001",
    "transaction_date": "2026-11-10 12:34:56",
    "menu_item_name": "Regular Ramen",
    "quantity": 2,
    "unit_price": 150.0,
    "total_amount": 300.0,
    "currency_code": "PHP",
    "payment_method": "Cash",
    "amount_tendered": 500.0,
    "change_amount": 200.0,
    "status": "completed",
    "cashier": "admin",
    "lines": [],
}


class Stage9ReceiptTests(unittest.TestCase):

    def test_receipt_contains_every_required_field(self) -> None:
        text = receipt.build_receipt(SAMPLE_SALE)
        for expected in (
            "RR-20261110-0001", "2026-11-10 12:34:56", "admin",
            "Regular Ramen", "2 x", "TOTAL", "Cash",
            "\u20b1150.00", "\u20b1300.00", "\u20b1500.00",
            "\u20b1200.00", "Tendered", "Change", "PHP",
        ):
            self.assertIn(expected, text, expected)

    def test_receipt_uses_peso_only(self) -> None:
        text = receipt.build_receipt(SAMPLE_SALE)
        self.assertIn("\u20b1", text)
        for forbidden in ("\u00a5", "\uffe5", "JPY", "CNY", "USD", "$"):
            self.assertNotIn(forbidden, text, forbidden)

    def test_receipt_lines_fit_a_32_column_thermal_roll(self) -> None:
        text = receipt.build_receipt(SAMPLE_SALE)
        for line in text.splitlines():
            self.assertLessEqual(len(line), receipt.RECEIPT_WIDTH, line)

    def test_reprint_marker_and_void_marker(self) -> None:
        self.assertIn(
            "REPRINT COPY",
            receipt.build_receipt(SAMPLE_SALE, reprint=True),
        )
        self.assertNotIn(
            "REPRINT COPY", receipt.build_receipt(SAMPLE_SALE)
        )
        voided = {
            **SAMPLE_SALE,
            "status": "voided",
            "voided_at": "2026-11-10 12:40:00",
            "void_reason": "Customer changed their mind immediately",
        }
        text = receipt.build_receipt(voided)
        self.assertIn("*** VOIDED ***", text)
        self.assertIn("SALE CANCELLED", text)
        self.assertIn("2026-11-10 12:40:00", text)
        # A long reason is deliberately word-wrapped for 58 mm paper.
        self.assertIn("Customer changed their mind", text)
        self.assertIn("immediately", text)
        for line in text.splitlines():
            self.assertLessEqual(len(line), receipt.RECEIPT_WIDTH, line)

    def test_receipt_filenames_are_distinct(self) -> None:
        number = "RR-20261110-0001"
        original = receipt.receipt_path(number)
        reprint = receipt.receipt_path(number, reprint=True)
        voided = receipt.receipt_path(number, voided=True)
        self.assertEqual(len({original, reprint, voided}), 3)
        self.assertTrue(original.name.endswith(".txt"))

    def test_archive_writes_utf8_with_the_peso_sign(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = receipt.archive_receipt(
                receipt.build_receipt(SAMPLE_SALE),
                SAMPLE_SALE["transaction_number"],
                base_dir=folder,
            )
            self.assertTrue(path.exists())
            self.assertIn("\u20b1", path.read_text(encoding="utf-8"))

    def test_issue_receipt_archives_even_when_printing_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            issued = receipt.issue_receipt(
                SAMPLE_SALE, base_dir=folder, auto_print=False
            )
            self.assertEqual(issued["print"]["method"], "skipped")
            self.assertTrue(issued["path"].exists())
            self.assertIn("PHP", issued["text"])

    def test_printer_failure_is_reported_and_never_raises(self) -> None:
        with patch.object(receipt.os, "startfile",
                          side_effect=OSError("no printer"), create=True):
            result = receipt.print_receipt(
                "sample", path=ROOT / "receipts" / "sample.txt"
            )
        self.assertFalse(result["ok"])
        self.assertIn("archived", result["detail"])


class Stage9ReceiptIntegrationTests(_TempDB):

    def test_printer_failure_never_rolls_back_a_committed_sale(self) -> None:
        self._stock(3)
        result = self._sale(1, "Cash", 200)
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(receipt.os, "startfile",
                              side_effect=OSError("printer offline"),
                              create=True):
                issued = receipt.issue_receipt(
                    result, base_dir=folder
                )
            self.assertFalse(issued["print"]["ok"])
            self.assertTrue(issued["path"].exists())
            self.assertIn(result["transaction_number"], issued["text"])
        detail = sales.get_sale_detail(result["sale_id"], self.db_path)
        self.assertEqual(detail["status"], "completed")
        self.assertEqual(self._counts()["sales_transactions"], 1)
        self.assertEqual(self._counts()["sale_deductions"], 8)

    def test_a_void_also_produces_a_voided_receipt(self) -> None:
        self._stock(3)
        result = self._sale(1, "Cash", 200)
        voided = sales.void_sale(result["sale_id"], "Cancel",
                                 db_path=self.db_path)
        with tempfile.TemporaryDirectory() as folder:
            issued = receipt.issue_receipt(
                voided, base_dir=folder, auto_print=False
            )
            self.assertTrue(issued["path"].exists())
            self.assertIn("VOIDED", issued["text"])
            self.assertTrue(issued["path"].name.endswith("-VOID.txt"))


# ======================================================================
# 7 - Shell registration and real database safety
# ======================================================================
class Stage9ShellTests(unittest.TestCase):

    def test_pos_page_is_registered_with_full_metadata(self) -> None:
        self.assertIn("pos", PAGES)
        meta = PAGES["pos"]
        self.assertEqual(meta["label"], "Point of Sale")
        self.assertTrue(meta["title"])
        self.assertTrue(meta["subtitle"])
        self.assertEqual(meta["view"].__name__, "PosView")
        self.assertTrue(callable(meta["view"].refresh))
        self.assertEqual(len(PAGES), 11)

    def test_main_entry_point_documents_the_ten_pages(self) -> None:
        source = _source("main.py")
        self.assertIn("11 pages", source)
        self.assertIn("Point of Sale", source)

    def test_stage9_adds_no_mandatory_third_party_dependency(self) -> None:
        requirements = _source("requirements.txt").lower()
        self.assertIn("customtkinter", requirements)
        self.assertIn("pillow", requirements)
        self.assertNotIn("pywin32", requirements)


class Stage9RealDatabaseTests(unittest.TestCase):

    def test_a_copy_of_the_real_database_migrates_cleanly(self) -> None:
        """Copy (never modify) the delivered DB and prove the launch-time
        migration will work on the user's real data."""
        self.assertTrue(REAL_DB.exists())
        before = hashlib.sha256(REAL_DB.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory() as folder:
            copy = Path(folder) / "ramen_inventory.db"
            shutil.copy2(REAL_DB, copy)
            with closing(sqlite3.connect(f"{copy.as_uri()}?mode=ro", uri=True)) as conn:
                original_hash = conn.execute(
                    "SELECT password_hash FROM users"
                ).fetchone()[0]
                original_count = conn.execute(
                    "SELECT COUNT(*) FROM ingredients"
                ).fetchone()[0]

            database.initialize_database(copy)

            with closing(database.get_connection(copy)) as conn:
                tables = {
                    row["name"] for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' "
                        "AND name NOT LIKE 'sqlite_%'"
                    )
                }
                # A bootstrapped real DB legitimately carries the additive
                # Stage 12 tables. Require the whole v4 core set and allow
                # ONLY those known extras (no surprise tables).
                self.assertTrue(ALL_TABLES <= tables)
                self.assertTrue(tables - ALL_TABLES <= STAGE12_TABLES)
                self.assertEqual(
                    conn.execute("PRAGMA integrity_check").fetchone()[0], "ok"
                )
                self.assertEqual(
                    list(conn.execute("PRAGMA foreign_key_check")), []
                )
                self.assertEqual(
                    conn.execute("PRAGMA user_version").fetchone()[0],
                    database.SCHEMA_VERSION,
                )
                self.assertEqual(
                    conn.execute("SELECT password_hash FROM users").fetchone()[0],
                    original_hash,
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM ingredients").fetchone()[0],
                    original_count,
                )
            self.assertEqual(sales.get_unit_price(copy), 150.0)
        self.assertEqual(
            hashlib.sha256(REAL_DB.read_bytes()).hexdigest(), before
        )

    def test_real_database_is_still_intact_in_read_only_mode(self) -> None:
        with closing(sqlite3.connect(f"{REAL_DB.as_uri()}?mode=ro", uri=True)) as conn:
            self.assertEqual(
                conn.execute("PRAGMA integrity_check").fetchone()[0], "ok"
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM menu_items").fetchone()[0], 1
            )
            self.assertEqual(
                conn.execute(
                    "SELECT name FROM menu_items WHERE is_active = 1"
                ).fetchone()[0],
                "Regular Ramen",
            )


if __name__ == "__main__":
    unittest.main()
