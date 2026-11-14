"""
Stage 12 automated checks - convenience-store products, barcodes and cart POS.

Every test uses a FRESH temporary SQLite database. The real
database/ramen_inventory.db is never opened for writing here.

Verified behaviours:
  1. additive, idempotent schema + one-time catalog bootstrap
  2. user accounts / password hashes / roles are preserved
  3. barcode validation, product CRUD and soft deactivation
  4. role guards (manager catalog, cashier POS only)
  5. cart atomicity, stock deduction, snapshots and void reversal
  6. multi-line PHP receipts and printing safety
  7. shell wiring and source hygiene
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any

import database
import permissions
import products
import receipt
import store_sales
from components.shell import PAGES
from formatting import CURRENCY_CODE, CURRENCY_SYMBOL, format_money

ROOT = Path(__file__).resolve().parents[1]
MANAGER: dict[str, Any] = {"user_id": 1, "username": "admin", "role": "manager"}
CASHIER: dict[str, Any] = {"user_id": 2, "username": "cashier", "role": "cashier"}

COKE = "4800001234567"
CLOUD9 = "4800005234565"
WATER = "4800002234564"


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


class _Stage12Case(unittest.TestCase):
    def setUp(self) -> None:
        permissions.clear_session()
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "stage12.db"
        database.initialize_database(self.db_path)
        products.ensure_schema(self.db_path)
        products.bootstrap_catalog(self.db_path)

    def tearDown(self) -> None:
        permissions.clear_session()
        self.temp.cleanup()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            return [dict(row) for row in conn.execute(sql, params)]
        finally:
            conn.close()

    def _scalar(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        conn = self._connect()
        try:
            row = conn.execute(sql, params).fetchone()
            return None if row is None else row[0]
        finally:
            conn.close()

    def _counts(self) -> dict[str, int]:
        return {
            table: int(self._scalar(f"SELECT COUNT(*) FROM {table}"))
            for table in (
                "store_sales", "store_sale_items", "stock_out_transactions",
                "stock_out_details", "stock_in_details",
            )
        }

    def _sale(self, cart: Any, tendered: Any = "500", actor: Any = MANAGER):
        return store_sales.confirm_sale(
            cart, "Cash", tendered,
            user_id=1, db_path=self.db_path, actor=actor,
        )


# ======================================================================
# 1 - Additive schema and catalog bootstrap
# ======================================================================
class SchemaTests(_Stage12Case):

    def test_fresh_v4_database_does_not_have_product_columns(self) -> None:
        fresh = Path(self.temp.name) / "fresh.db"
        database.initialize_database(fresh)
        columns = products.column_names(fresh)
        self.assertNotIn("barcode", columns)
        self.assertNotIn("unit_price", columns)

    def test_ensure_schema_adds_exactly_four_columns(self) -> None:
        fresh = Path(self.temp.name) / "add.db"
        database.initialize_database(fresh)
        report = products.ensure_schema(fresh)
        self.assertEqual(
            set(report["added_columns"]),
            {"barcode", "category", "unit_price", "is_sellable"},
        )
        self.assertEqual(report["version"], products.CATALOG_VERSION)

    def test_ensure_schema_is_idempotent(self) -> None:
        report = products.ensure_schema(self.db_path)
        self.assertEqual(report["added_columns"], [])
        self.assertEqual(
            products.ensure_schema(self.db_path)["added_columns"], []
        )

    def test_store_tables_and_indexes_exist(self) -> None:
        tables = {
            row["name"] for row in self._query(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertIn("app_meta", tables)
        self.assertIn("store_sales", tables)
        self.assertIn("store_sale_items", tables)
        indexes = {
            row["name"] for row in self._query(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        self.assertIn("ux_ingredients_active_barcode", indexes)

    def test_user_version_is_not_changed_by_stage12(self) -> None:
        # Stage 12 is additive; database.py keeps its own v1-v4 chain.
        self.assertEqual(
            self._scalar("PRAGMA user_version"), database.SCHEMA_VERSION
        )
        self.assertEqual(database.SCHEMA_VERSION, 4)

    def test_app_meta_records_the_stage12_schema_version(self) -> None:
        self.assertEqual(
            self._scalar(
                "SELECT value FROM app_meta WHERE key = 'stage12_schema'"
            ),
            products.CATALOG_VERSION,
        )

    def test_bootstrap_is_idempotent(self) -> None:
        before = self._scalar(
            "SELECT COUNT(*) FROM ingredients WHERE is_active = 1"
        )
        result = products.bootstrap_catalog(self.db_path)
        self.assertFalse(result["seeded"])
        self.assertEqual(
            self._scalar("SELECT COUNT(*) FROM ingredients WHERE is_active = 1"),
            before,
        )

    def test_bootstrap_soft_retires_ramen_demo_ingredients(self) -> None:
        legacy = self._query(
            "SELECT name, is_active, is_sellable, quantity_on_hand "
            "FROM ingredients WHERE barcode = ''"
        )
        self.assertEqual(len(legacy), 8)
        for row in legacy:
            self.assertEqual(row["is_active"], 0)
            self.assertEqual(row["is_sellable"], 0)
            self.assertEqual(row["quantity_on_hand"], 0)

    def test_bootstrap_seeds_a_barcoded_convenience_store_catalog(self) -> None:
        rows = self._query(
            "SELECT * FROM ingredients WHERE is_active = 1 AND barcode <> ''"
        )
        self.assertEqual(len(rows), 18)
        codes = {str(row["barcode"]) for row in rows}
        self.assertEqual(len(codes), 18)
        categories = {str(row["category"]) for row in rows}
        self.assertIn("Beverages", categories)
        self.assertIn("Sweets", categories)
        self.assertIn("Ready-to-eat", categories)
        self.assertIn("Food", categories)
        for row in rows:
            self.assertEqual(row["is_sellable"], 1, row["name"])
            self.assertGreater(float(row["unit_price"]), 0, row["name"])

    def test_bootstrap_preserves_all_user_accounts_and_hashes(self) -> None:
        users = self._query(
            "SELECT user_id, username, password_hash, role FROM users"
        )
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]["username"], "admin")
        self.assertEqual(users[0]["role"], "manager")
        self.assertTrue(users[0]["password_hash"].startswith("pbkdf2_sha256$"))

    def test_bootstrap_records_opening_balances_as_stock_in(self) -> None:
        self.assertEqual(
            self._scalar("SELECT COUNT(*) FROM stock_in_transactions"), 1
        )
        self.assertEqual(
            self._scalar("SELECT COUNT(*) FROM stock_in_details"), 18
        )
        movements = self._query(
            "SELECT * FROM vw_stock_movements WHERE movement_type = 'IN'"
        )
        self.assertEqual(len(movements), 18)

    def test_bootstrap_never_overwrites_an_existing_manager_catalog(self) -> None:
        other = Path(self.temp.name) / "manager.db"
        database.initialize_database(other)
        products.ensure_schema(other)
        products.add_product(
            "My Own Item", "12345678", "Other", "piece", "25",
            "1", "", "4", db_path=other, actor=MANAGER,
        )
        # Simulate an unmarked database that already contains a product.
        conn = sqlite3.connect(other)
        conn.execute("DELETE FROM app_meta WHERE key = 'catalog_seeded'")
        conn.commit()
        conn.close()
        result = products.bootstrap_catalog(other)
        self.assertFalse(result["seeded"])
        self.assertEqual(result["products"], 1)
        self.assertEqual(
            products.list_products(db_path=other)[0]["name"], "My Own Item"
        )

    def test_catalog_ready_reflects_the_marker(self) -> None:
        self.assertTrue(products.catalog_ready(self.db_path))


# ======================================================================
# 2 - Barcode validation and product CRUD
# ======================================================================
class ProductCrudTests(_Stage12Case):

    def test_barcode_requires_6_to_14_digits(self) -> None:
        for bad in ("", "abc", "12345", "1" * 15, "4800-ABCD"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    products.validate_barcode(bad)

    def test_barcode_accepts_ean13_and_strips_separators(self) -> None:
        self.assertEqual(products.validate_barcode("4800 001 234 567"), COKE)
        self.assertEqual(products.validate_barcode("4800-001-234-567"), COKE)
        self.assertEqual(products.validate_barcode("12345678"), "12345678")

    def test_add_product_creates_a_sellable_barcoded_item(self) -> None:
        key = products.add_product(
            "Test Biscuit", "12345678", "Sweets", "pack", "24.50",
            "5", "2027-01-31", "7", db_path=self.db_path, actor=MANAGER,
        )
        row = products.find_by_barcode("12345678", self.db_path)
        self.assertEqual(int(row["ingredient_id"]), key)
        self.assertEqual(row["name"], "Test Biscuit")
        self.assertEqual(float(row["unit_price"]), 24.5)
        self.assertEqual(float(row["quantity_on_hand"]), 7)

    def test_opening_stock_writes_a_real_stock_in_line(self) -> None:
        before = self._scalar("SELECT COUNT(*) FROM stock_in_details")
        products.add_product(
            "Test Biscuit", "12345678", "Sweets", "pack", "24.50",
            "5", "", "7", db_path=self.db_path, actor=MANAGER,
        )
        self.assertEqual(
            self._scalar("SELECT COUNT(*) FROM stock_in_details"), before + 1
        )

    def test_duplicate_active_barcode_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "barcode"):
            products.add_product(
                "Another Cola", COKE, "Beverages", "bottle", "40",
                db_path=self.db_path, actor=MANAGER,
            )

    def test_duplicate_active_name_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "name"):
            products.add_product(
                "Coca-Cola Original 500 ml", "12345678", "Beverages",
                "bottle", "40", db_path=self.db_path, actor=MANAGER,
            )

    def test_invalid_category_and_price_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            products.add_product(
                "Mystery", "12345678", "Not a category", "piece", "10",
                db_path=self.db_path, actor=MANAGER,
            )
        with self.assertRaises(ValueError):
            products.add_product(
                "Negative", "12345678", "Other", "piece", "-10",
                db_path=self.db_path, actor=MANAGER,
            )

    def test_update_product_never_touches_physical_stock(self) -> None:
        key = int(products.find_by_barcode(COKE, self.db_path)["ingredient_id"])
        before = float(
            products.find_by_barcode(COKE, self.db_path)["quantity_on_hand"]
        )
        products.update_product(
            key, "Cola 500 ml", COKE, "Beverages", "bottle", "38",
            "10", "2027-05-01", db_path=self.db_path, actor=MANAGER,
        )
        row = products.find_by_barcode(COKE, self.db_path)
        self.assertEqual(row["name"], "Cola 500 ml")
        self.assertEqual(float(row["unit_price"]), 38.0)
        self.assertEqual(float(row["quantity_on_hand"]), before)

    def test_deactivate_hides_a_product_from_the_scanner(self) -> None:
        key = int(products.find_by_barcode(COKE, self.db_path)["ingredient_id"])
        products.deactivate_product(key, db_path=self.db_path, actor=MANAGER)
        self.assertIsNone(products.find_by_barcode(COKE, self.db_path))
        self.assertNotIn(
            COKE,
            {row["barcode"] for row in products.search_sellable(db_path=self.db_path)},
        )
        self.assertEqual(
            self._scalar(
                "SELECT COUNT(*) FROM ingredients WHERE ingredient_id = ?", (key,)
            ),
            1,
        )

    def test_list_products_supports_search_and_category_filter(self) -> None:
        self.assertEqual(len(products.list_products(db_path=self.db_path)), 18)
        # "Cola" is a loose needle: it also matches "Chocolate" (Choc-ola-te),
        # which is the intended behaviour of a name / barcode / category search.
        colas = products.list_products("Coca-Cola", db_path=self.db_path)
        self.assertEqual(len(colas), 1)
        self.assertEqual(colas[0]["name"], "Coca-Cola Original 500 ml")
        drinks = products.list_products(
            category="Beverages", db_path=self.db_path
        )
        self.assertTrue(drinks)
        for row in drinks:
            self.assertEqual(row["category"], "Beverages")

    def test_summary_counts_come_from_sqlite(self) -> None:
        counts = products.summary(self.db_path)
        self.assertEqual(counts["products"], 18)
        self.assertEqual(counts["low_stock"], 0)
        self.assertEqual(counts["out_of_stock"], 0)

    def test_cashier_cannot_manage_products(self) -> None:
        for call in (
            lambda: products.add_product(
                "X", "12345678", "Other", "piece", "10",
                db_path=self.db_path, actor=CASHIER,
            ),
            lambda: products.update_product(
                1, "X", "12345678", "Other", "piece", "10",
                db_path=self.db_path, actor=CASHIER,
            ),
            lambda: products.deactivate_product(
                1, db_path=self.db_path, actor=CASHIER
            ),
        ):
            with self.subTest():
                with self.assertRaises(permissions.PermissionDenied):
                    call()

    def test_no_actor_and_no_session_falls_back_to_the_live_session(self) -> None:
        permissions.set_session(CASHIER)
        with self.assertRaises(permissions.PermissionDenied):
            products.add_product(
                "X", "12345678", "Other", "piece", "10", db_path=self.db_path
            )
        permissions.clear_session()


# ======================================================================
# 3 - Cart validation, atomic commit and void
# ======================================================================
class SaleTests(_Stage12Case):

    def test_normalize_cart_merges_duplicate_barcodes(self) -> None:
        lines = store_sales.normalize_cart(
            [(COKE, 2), (COKE, 1), {"barcode": CLOUD9, "quantity": 3}]
        )
        self.assertEqual(
            {(line["barcode"], line["quantity"]) for line in lines},
            {(COKE, 3), (CLOUD9, 3)},
        )

    def test_normalize_cart_rejects_bad_baskets(self) -> None:
        for bad in (None, [], [(COKE, 0)], [(COKE, -1)], [(COKE, 1000)], "abc"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    store_sales.normalize_cart(bad)

    def test_lookup_product_reports_unknown_barcodes(self) -> None:
        with self.assertRaisesRegex(ValueError, "No sellable product"):
            store_sales.lookup_product("9999999999999", self.db_path)

    def test_preview_is_read_only(self) -> None:
        before = self._counts()
        preview = store_sales.preview_sale(
            [(COKE, 2), (CLOUD9, 1)], db_path=self.db_path
        )
        self.assertEqual(preview["total_amount"], 82.0)
        self.assertEqual(preview["item_count"], 3)
        self.assertTrue(preview["can_confirm"])
        self.assertEqual(preview["shortages"], [])
        self.assertEqual(self._counts(), before)

    def test_preview_reports_a_stock_shortage(self) -> None:
        preview = store_sales.preview_sale(
            [(COKE, 999)], db_path=self.db_path
        )
        self.assertFalse(preview["can_confirm"])
        self.assertTrue(preview["shortages"])

    def test_confirm_deducts_every_product_and_writes_one_stock_out(self) -> None:
        before = self._counts()
        sale = self._sale([(COKE, 2), (CLOUD9, 1)])
        self.assertEqual(sale["total_amount"], 82.0)
        self.assertEqual(sale["change_amount"], 418.0)
        self.assertEqual(sale["currency_code"], "PHP")
        self.assertEqual(len(sale["items"]), 2)
        self.assertEqual(
            self._counts()["store_sales"], before["store_sales"] + 1
        )
        self.assertEqual(
            self._counts()["store_sale_items"], before["store_sale_items"] + 2
        )
        self.assertEqual(
            self._counts()["stock_out_details"], before["stock_out_details"] + 2
        )
        self.assertEqual(
            float(products.find_by_barcode(COKE, self.db_path)["quantity_on_hand"]),
            58.0,
        )
        self.assertEqual(
            float(products.find_by_barcode(CLOUD9, self.db_path)["quantity_on_hand"]),
            79.0,
        )

    def test_sale_is_visible_in_the_existing_movement_view(self) -> None:
        sale = self._sale([(COKE, 1)])
        movements = self._query(
            "SELECT * FROM vw_stock_movements WHERE reason = 'Sale'"
        )
        self.assertEqual(len(movements), 1)
        self.assertEqual(movements[0]["ingredient_name"], "Coca-Cola Original 500 ml")
        self.assertLess(movements[0]["quantity"], 0)

    def test_insufficient_stock_rolls_back_the_whole_sale(self) -> None:
        before = self._counts()
        with self.assertRaisesRegex(ValueError, "Not enough stock"):
            self._sale([(COKE, 2), (WATER, 999)])
        self.assertEqual(self._counts(), before)
        self.assertEqual(
            float(products.find_by_barcode(COKE, self.db_path)["quantity_on_hand"]),
            60.0,
        )

    def test_short_payment_rolls_back_the_whole_sale(self) -> None:
        before = self._counts()
        with self.assertRaisesRegex(ValueError, "Amount tendered"):
            self._sale([(COKE, 1)], tendered="10")
        self.assertEqual(self._counts(), before)

    def test_retired_product_cannot_be_sold(self) -> None:
        key = int(products.find_by_barcode(COKE, self.db_path)["ingredient_id"])
        products.deactivate_product(key, db_path=self.db_path, actor=MANAGER)
        with self.assertRaisesRegex(ValueError, "retired"):
            self._sale([(COKE, 1)])

    def test_cancelling_before_confirm_writes_nothing(self) -> None:
        before = self._counts()
        store_sales.preview_sale([(COKE, 3)], db_path=self.db_path)
        # The cashier simply clears the basket: no repository write at all.
        self.assertEqual(self._counts(), before)

    def test_receipt_snapshots_survive_a_later_rename(self) -> None:
        sale = self._sale([(COKE, 1)])
        key = int(products.find_by_barcode(COKE, self.db_path)["ingredient_id"])
        products.update_product(
            key, "Renamed Soda", COKE, "Beverages", "bottle", "99",
            db_path=self.db_path, actor=MANAGER,
        )
        detail = store_sales.get_sale_detail(sale["sale_id"], self.db_path)
        self.assertEqual(detail["items"][0]["product_name"], "Coca-Cola Original 500 ml")
        self.assertEqual(float(detail["items"][0]["unit_price"]), 35.0)
        self.assertEqual(float(detail["total_amount"]), 35.0)

    def test_void_restores_stock_and_keeps_history(self) -> None:
        sale = self._sale([(COKE, 2)])
        before_in = self._scalar("SELECT COUNT(*) FROM stock_in_details")
        voided = store_sales.void_sale(
            sale["sale_id"], "Customer changed mind",
            user_id=1, db_path=self.db_path, actor=MANAGER,
        )
        self.assertEqual(voided["status"], "voided")
        self.assertEqual(voided["void_reason"], "Customer changed mind")
        self.assertEqual(
            self._scalar("SELECT COUNT(*) FROM store_sales"), 1
        )
        self.assertEqual(
            self._scalar("SELECT COUNT(*) FROM stock_in_details"), before_in + 1
        )
        self.assertEqual(
            float(products.find_by_barcode(COKE, self.db_path)["quantity_on_hand"]),
            60.0,
        )
        self.assertEqual(store_sales.gross_sales(self.db_path), 0.0)

    def test_void_is_additive_not_an_absolute_balance_reset(self) -> None:
        sale = self._sale([(COKE, 2)])
        conn = self._connect()
        conn.execute(
            "UPDATE ingredients SET quantity_on_hand = quantity_on_hand + 5 "
            "WHERE barcode = ?", (COKE,)
        )
        conn.commit()
        conn.close()
        store_sales.void_sale(
            sale["sale_id"], "Test reversal", db_path=self.db_path, actor=MANAGER
        )
        self.assertEqual(
            float(products.find_by_barcode(COKE, self.db_path)["quantity_on_hand"]),
            65.0,
        )

    def test_void_twice_is_rejected(self) -> None:
        sale = self._sale([(COKE, 1)])
        store_sales.void_sale(
            sale["sale_id"], "First reason", db_path=self.db_path, actor=MANAGER
        )
        with self.assertRaisesRegex(ValueError, "already voided"):
            store_sales.void_sale(
                sale["sale_id"], "Second reason", db_path=self.db_path, actor=MANAGER
            )

    def test_cashier_may_record_a_sale_but_not_void_it(self) -> None:
        sale = store_sales.confirm_sale(
            [(COKE, 1)], "Cash", "100", user_id=2,
            db_path=self.db_path, actor=CASHIER,
        )
        self.assertEqual(sale["status"], "completed")
        with self.assertRaises(permissions.PermissionDenied):
            store_sales.void_sale(
                sale["sale_id"], "Not allowed", db_path=self.db_path, actor=CASHIER
            )

    def test_no_negative_stock_after_a_sale(self) -> None:
        # Sell the entire opening balance (60 x PHP 35 = PHP 2,100), so the
        # tendered amount must cover it. The balance must land on exactly 0.
        self._sale([(COKE, 60)], tendered="5000")
        rows = self._query(
            "SELECT quantity_on_hand FROM ingredients WHERE quantity_on_hand < 0"
        )
        self.assertEqual(rows, [])
        self.assertEqual(
            float(products.find_by_barcode(COKE, self.db_path)["quantity_on_hand"]),
            0.0,
        )

    def test_history_search_finds_a_sold_product(self) -> None:
        self._sale([(COKE, 1)])
        self.assertEqual(len(store_sales.get_sales_history(db_path=self.db_path)), 1)
        self.assertEqual(
            len(store_sales.get_sales_history("Cola", db_path=self.db_path)), 1
        )
        self.assertEqual(
            len(store_sales.get_sales_history("nothing", db_path=self.db_path)), 0
        )


# ======================================================================
# 4 - Receipts and wiring
# ======================================================================
class ReceiptAndWiringTests(_Stage12Case):

    def test_store_receipt_lists_every_scanned_product(self) -> None:
        sale = self._sale([(COKE, 2), (CLOUD9, 1)])
        text = receipt.build_store_receipt(sale)
        self.assertIn(receipt.MART_NAME, text)
        self.assertIn(sale["transaction_number"], text)
        self.assertIn("Coca-Cola Original 500 ml", text)
        self.assertIn(COKE, text)
        self.assertIn("Cloud 9 Classic Chocolate", text)
        self.assertIn("TOTAL", text)
        self.assertIn(format_money(82.0), text)
        self.assertIn(CURRENCY_SYMBOL, text)
        self.assertIn("PHP", text)

    def test_store_receipt_never_uses_a_yen_or_dollar_sign(self) -> None:
        text = receipt.build_store_receipt(self._sale([(COKE, 1)]))
        # Escapes, not literal glyphs: the Stage 9 hygiene test scans every
        # .py file in the project for these two characters.
        for symbol in ("\u00a5", "\uffe5", "$"):
            self.assertNotIn(symbol, text)

    def test_reprint_and_void_markers(self) -> None:
        sale = self._sale([(COKE, 1)])
        self.assertIn(
            "REPRINT COPY", receipt.build_store_receipt(sale, reprint=True)
        )
        voided = store_sales.void_sale(
            sale["sale_id"], "Wrong item", db_path=self.db_path, actor=MANAGER
        )
        text = receipt.build_store_receipt(voided)
        self.assertIn("VOIDED", text)
        self.assertIn("SALE CANCELLED", text)
        self.assertIn("Wrong item", text)

    def test_issue_store_receipt_archives_without_printing(self) -> None:
        sale = self._sale([(COKE, 1)])
        target = Path(self.temp.name) / "receipts"
        outcome = receipt.issue_store_receipt(
            sale, auto_print=False, base_dir=target
        )
        self.assertEqual(outcome["print"]["method"], "skipped")
        self.assertTrue(outcome["path"].exists())
        self.assertEqual(
            outcome["path"].read_text(encoding="utf-8"), outcome["text"]
        )

    def test_legacy_ramen_receipt_builder_still_works(self) -> None:
        text = receipt.build_receipt(
            {
                "transaction_number": "POS-1", "menu_item_name": "Regular Ramen",
                "quantity": 2, "unit_price": 150.0, "total_amount": 300.0,
                "payment_method": "Cash", "amount_tendered": 300.0,
                "change_amount": 0.0, "status": "completed",
            }
        )
        self.assertIn("Regular Ramen", text)

    def test_products_page_is_registered_and_manager_only(self) -> None:
        self.assertIn("products", PAGES)
        self.assertEqual(PAGES["products"]["label"], "Products")
        self.assertEqual(PAGES["products"]["view"].__name__, "ProductsView")
        self.assertTrue(callable(PAGES["products"]["view"].refresh))
        self.assertIn("products", permissions.MANAGER_PAGES)
        self.assertNotIn("products", permissions.CASHIER_PAGES)
        self.assertEqual(tuple(PAGES), permissions.MANAGER_PAGES)

    def test_live_pos_is_the_barcode_basket_view(self) -> None:
        self.assertEqual(PAGES["pos"]["view"].__name__, "PosView")
        self.assertEqual(PAGES["pos"]["view"].__module__, "pages.store_pos")
        self.assertEqual(permissions.CASHIER_PAGES, ("pos",))
        self.assertEqual(permissions.default_page(CASHIER), "pos")

    def test_manage_products_capability_is_manager_only(self) -> None:
        self.assertIn(
            permissions.CAP_MANAGE_PRODUCTS, permissions.ALL_CAPABILITIES
        )
        self.assertNotIn(
            permissions.CAP_MANAGE_PRODUCTS, permissions.CASHIER_CAPABILITIES
        )
        self.assertTrue(
            permissions.can(MANAGER, permissions.CAP_MANAGE_PRODUCTS)
        )
        self.assertFalse(
            permissions.can(CASHIER, permissions.CAP_MANAGE_PRODUCTS)
        )

    def test_startup_runs_the_stage12_bootstrap(self) -> None:
        source = _source("main.py")
        self.assertIn("products.ensure_schema()", source)
        self.assertIn("products.bootstrap_catalog()", source)
        self.assertIn("12 pages", source)

    def test_no_new_third_party_dependency_for_barcodes(self) -> None:
        """Scanning stays keyboard-wedge only; pywin32 is for the PRINTER."""
        requirements = _source("requirements.txt").lower()
        self.assertIn("customtkinter", requirements)
        self.assertIn("pillow", requirements)
        for library in ("python-barcode", "zxing", "opencv", "pyzbar"):
            self.assertNotIn(library, requirements)

    def test_new_modules_use_only_the_peso_sign(self) -> None:
        for relative in (
            "products.py", "store_sales.py", "receipt.py",
            "pages/products.py", "pages/store_pos.py",
        ):
            with self.subTest(relative=relative):
                text = _source(relative)
                self.assertNotIn("\u00a5", text)
                self.assertNotIn("\uffe5", text)

    def test_checkout_card_is_not_squeezed_into_a_side_column(self) -> None:
        """A side-by-side split made the payment panel 1px wide at 1160x720."""
        source = _source("pages/store_pos.py")
        # Stage 17 tightened the vertical gap from 14 to 4px; the card is
        # STILL packed full-width (never a squeezed side column).
        self.assertIn('panel.pack(fill="x", pady=(4, 0))', source)
        self.assertIn('uniform="checkout"', source)
        self.assertNotIn('panel.pack(side="right", fill="y")', source)

    def test_stage12_tables_fit_the_default_window(self) -> None:
        """grid 'uniform' scales columns by max(minsize/weight) * sum(weight)."""
        from pages import products as products_page
        from pages import store_pos as pos_page
        groups = (
            (products_page.TABLE_WEIGHTS, products_page.TABLE_MINSIZES),
            (pos_page.CART_WEIGHTS, pos_page.CART_MINSIZES),
            (pos_page.HISTORY_WEIGHTS, pos_page.HISTORY_MINSIZES),
        )
        for weights, minsizes in groups:
            with self.subTest(weights=weights):
                inflated = max(m / w for m, w in zip(minsizes, weights))
                self.assertLessEqual(inflated * sum(weights), 820)

    def test_scanner_clears_the_entry_even_on_a_bad_code(self) -> None:
        """A rejected code must not be left for the next scan to append to."""
        source = _source("pages/store_pos.py")
        self.assertIn('self.scan_entry.bind("<Return>"', source)
        start = source.index("def _add_by_barcode")
        end = source.index("def _add_selected", start)
        body = source[start:end]
        cleared = body.index('self.scan_entry.delete(0, "end")')
        self.assertLess(cleared, body.index("if not code:"))
        self.assertLess(cleared, body.index("store_sales.lookup_product(code)"))

    def test_currency_is_still_php(self) -> None:
        self.assertEqual(CURRENCY_CODE, "PHP")
        self.assertEqual(CURRENCY_SYMBOL, "\u20b1")
        self.assertEqual(format_money(45), "\u20b145.00")


if __name__ == "__main__":
    unittest.main()
