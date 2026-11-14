"""
Stage 16 automated checks - automatic change ("auto deduct") + quick cash.

VERIFIED BEHAVIOURS
  1. change.py          - pure auto-deduct logic, centavo rounding, status
                          labels, suggested bill/coin breakdown, quick bills
  2. money rules        - parsing/rounding MATCH products.validate_money() and
                          store_sales.confirm_sale(); the NET discounted total
                          is used, never the pre-discount subtotal
  3. real sale          - 1 x Boy Bawang (PHP 18.00) + PHP 100 cash =>
                          PHP 82.00 change, ONE atomic sale, stock -1,
                          receipt archived with the change line
  4. short payment      - live preview says "Short by", writes NO sale rows
  5. POS wiring         - StringVar trace, quick-cash buttons, live label,
                          no placeholder, post-sale reset, display only
  6. hygiene            - no new dependency, no schema change, PHP/₱ only,
                          no dynamic execution, no SQL in the pure module

Every transactional test uses a FRESH temporary SQLite database. The delivered
database/ramen_inventory.db is never opened for writing here.
"""

from __future__ import annotations

import ast
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any

import change
import database
import discounts
import permissions
import products
import receipt
import store_sales
from formatting import CURRENCY_SYMBOL, format_money

ROOT = Path(__file__).resolve().parents[1]
CASHIER: dict[str, Any] = {"user_id": 2, "username": "cashier", "role": "cashier"}

# Boy Bawang Garlic 50 g - PHP 18.00. The exact example the user gave.
BOY_BAWANG = "4800010234560"
PRICE = 18.00
CASH = "100"
EXPECTED_CHANGE = 82.00


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _has_dynamic_execution(source: str) -> bool:
    """Look for real CALL nodes, not the word 'eval' in a docstring."""
    tree = ast.parse(source)
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in ("eval", "exec", "compile")
        for node in ast.walk(tree)
    )


def _code_without_strings(source: str) -> str:
    """Executable code with every string literal (incl. docstrings) blanked."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            node.value = ""
    return ast.unparse(tree)


class ChangeLogicTests(unittest.TestCase):

    def test_the_users_exact_example_18_peso_sale_with_100_peso_bill(self) -> None:
        info = change.compute(PRICE, CASH)
        self.assertEqual(info["status"], change.STATUS_DUE)
        self.assertEqual(info["total"], 18.0)
        self.assertEqual(info["tendered"], 100.0)
        self.assertEqual(info["change"], EXPECTED_CHANGE)
        self.assertEqual(info["short"], 0.0)
        self.assertTrue(info["ok"])
        self.assertEqual(info["label"], f"Change due: {format_money(82)}")

    def test_change_is_formatted_as_peso_not_a_bare_number(self) -> None:
        label = change.compute(PRICE, CASH)["label"]
        self.assertIn(CURRENCY_SYMBOL, label)
        self.assertIn("82.00", label)
        self.assertIn("PHP", _source("formatting.py"))

    def test_exact_payment_reports_no_change(self) -> None:
        info = change.compute(18.0, "18")
        self.assertEqual(info["status"], change.STATUS_EXACT)
        self.assertEqual(info["change"], 0.0)
        self.assertTrue(info["ok"])
        self.assertIn("no change", info["label"].lower())
        self.assertEqual(info["detail"], "")

    def test_short_payment_reports_the_shortfall(self) -> None:
        info = change.compute(18.0, "10")
        self.assertEqual(info["status"], change.STATUS_INSUFFICIENT)
        self.assertEqual(info["short"], 8.0)
        self.assertEqual(info["change"], 0.0)
        self.assertFalse(info["ok"])
        self.assertIn(format_money(8), info["label"])
        self.assertIn("more cash", info["detail"])

    def test_empty_basket_has_no_change_to_show(self) -> None:
        info = change.compute(0.0, "100")
        self.assertEqual(info["status"], change.STATUS_NO_BASKET)
        self.assertFalse(info["ok"])
        self.assertEqual(info["change"], 0.0)

    def test_blank_tendered_field_shows_a_hint_not_an_error(self) -> None:
        info = change.compute(18.0, "")
        self.assertEqual(info["status"], change.STATUS_EMPTY)
        self.assertFalse(info["ok"])
        self.assertIn("Tap a bill", info["label"])

    def test_whitespace_only_is_treated_as_blank(self) -> None:
        self.assertEqual(change.compute(18.0, "   ")["status"], change.STATUS_EMPTY)

    def test_non_numeric_input_is_reported_not_raised(self) -> None:
        for bad in ("abc", "12abc", "1,000", "NaN", "inf", "-5"):
            with self.subTest(bad=bad):
                info = change.compute(18.0, bad)
                self.assertIn(
                    info["status"],
                    (change.STATUS_INVALID, change.STATUS_INSUFFICIENT),
                )
                self.assertFalse(info["ok"])

    def test_compute_never_raises_on_garbage(self) -> None:
        for value in (None, object(), [], {}, float("nan"), -1, "-1"):
            info = change.compute(18.0, value)
            self.assertIn("status", info)
            self.assertIn("label", info)

    def test_negative_total_is_treated_as_no_basket(self) -> None:
        self.assertEqual(
            change.compute(-5, "100")["status"], change.STATUS_NO_BASKET
        )

    def test_surrounding_whitespace_is_accepted(self) -> None:
        self.assertEqual(change.compute(18.0, "  100  ")["change"], 82.0)

    def test_centavo_amounts_are_rounded_to_two_decimals(self) -> None:
        info = change.compute(18.25, "20.50")
        self.assertEqual(info["change"], 2.25)
        self.assertEqual(info["total"], 18.25)

    def test_change_uses_the_net_discounted_total_not_the_subtotal(self) -> None:
        # PHP 18.00 less a 20% Senior discount = PHP 14.40 net.
        discounted = discounts.compute(18.0, discounts.SENIOR)
        info = change.compute(discounted["total_amount"], "100")
        self.assertEqual(info["total"], 14.40)
        self.assertEqual(info["change"], 85.60)
        self.assertNotEqual(info["change"], 82.0)

    def test_round_money_matches_store_sales_rounding(self) -> None:
        self.assertEqual(change.round_money(1.005), store_sales._money(1.005))
        self.assertEqual(change.round_money(82.0), store_sales._money(82.0))

    def test_parse_tendered_matches_products_validate_money(self) -> None:
        for value in ("100", "18.5", "0", " 42 ", "1000"):
            with self.subTest(value=value):
                self.assertEqual(
                    change.parse_tendered(value),
                    products.validate_money(value, "Amount tendered"),
                )

    def test_parse_tendered_rejects_the_same_bad_values(self) -> None:
        for value in ("abc", "-1", "nan", "inf", ""):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    products.validate_money(value, "Amount tendered")
                with self.assertRaises(ValueError):
                    change.parse_tendered(value)

    def test_good_and_bad_status_sets_are_public(self) -> None:
        self.assertIn(change.STATUS_DUE, change.GOOD_STATUSES)
        self.assertIn(change.STATUS_EXACT, change.GOOD_STATUSES)
        self.assertIn(change.STATUS_INSUFFICIENT, change.BAD_STATUSES)
        self.assertIn(change.STATUS_INVALID, change.BAD_STATUSES)


class QuickCashTests(unittest.TestCase):

    def test_quick_cash_includes_the_100_peso_bill(self) -> None:
        self.assertIn(100, change.QUICK_CASH)

    def test_quick_cash_is_ordered_small_to_large(self) -> None:
        self.assertEqual(change.QUICK_CASH, tuple(sorted(change.QUICK_CASH)))

    def test_quick_cash_buttons_are_peso_labelled(self) -> None:
        self.assertEqual(change.quick_cash_label(100), f"{CURRENCY_SYMBOL}100")
        self.assertEqual(change.quick_cash_label(1000), f"{CURRENCY_SYMBOL}1,000")

    def test_quick_cash_values_are_plain_numbers_for_the_entry(self) -> None:
        # The label has a thousands separator, but the value written into the
        # entry must stay float()-parseable by products.validate_money().
        for amount in change.QUICK_CASH:
            with self.subTest(amount=amount):
                self.assertEqual(
                    change.parse_tendered(str(amount)), float(amount)
                )
                self.assertNotIn(",", str(amount))

    def test_every_quick_bill_covers_a_small_basket(self) -> None:
        for amount in change.QUICK_CASH:
            with self.subTest(amount=amount):
                info = change.compute(PRICE, str(amount))
                self.assertTrue(info["ok"])
                self.assertEqual(info["change"], round(amount - PRICE, 2))


class BreakdownTests(unittest.TestCase):

    def test_82_pesos_breaks_into_real_bills(self) -> None:
        self.assertEqual(
            change.breakdown(82),
            [("50", 1), ("20", 1), ("10", 1), ("1", 2)],
        )

    def test_breakdown_is_shown_in_the_change_detail(self) -> None:
        detail = change.compute(18, "100")["detail"]
        self.assertIn("Give back:", detail)
        self.assertIn("1 x " + CURRENCY_SYMBOL + "50", detail)
        self.assertIn("2 x " + CURRENCY_SYMBOL + "1", detail)

    def test_breakdown_handles_centavos(self) -> None:
        self.assertEqual(change.breakdown(0.25), [("0.25", 1)])
        self.assertEqual(change.breakdown(0.41), [("0.25", 1), ("0.10", 1), ("0.05", 1), ("0.01", 1)])
        self.assertIn(CURRENCY_SYMBOL + "0.25", change.format_breakdown(0.25))

    def test_breakdown_of_zero_is_empty(self) -> None:
        self.assertEqual(change.breakdown(0), [])
        self.assertEqual(change.format_breakdown(0), "")

    def test_breakdown_sums_back_to_the_change(self) -> None:
        values = {"1,000": 1000.0, "500": 500.0, "200": 200.0,
                  "100": 100.0, "50": 50.0, "20": 20.0, "10": 10.0,
                  "5": 5.0, "1": 1.0, "0.25": 0.25, "0.10": 0.10,
                  "0.05": 0.05, "0.01": 0.01}
        for amount in (82.0, 137.65, 999.99, 0.37, 1000.0):
            with self.subTest(amount=amount):
                total = sum(values[label] * count
                            for label, count in change.breakdown(amount))
                self.assertAlmostEqual(total, amount, places=2)

    def test_no_change_has_no_breakdown_detail(self) -> None:
        self.assertEqual(change.compute(18, "18")["detail"], "")


class _DbCase(unittest.TestCase):
    def setUp(self) -> None:
        permissions.clear_session()
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "stage16.db"
        database.initialize_database(self.db_path)
        products.ensure_schema(self.db_path)
        products.bootstrap_catalog(self.db_path)

    def tearDown(self) -> None:
        permissions.clear_session()
        self.temp.cleanup()

    def _stock(self) -> float:
        conn = sqlite3.connect(self.db_path)
        try:
            return float(conn.execute(
                "SELECT quantity_on_hand FROM ingredients WHERE barcode = ?",
                (BOY_BAWANG,),
            ).fetchone()[0])
        finally:
            conn.close()

    def _sale(self, tendered: str, **kwargs: Any) -> dict[str, Any]:
        return store_sales.confirm_sale(
            [{"barcode": BOY_BAWANG, "quantity": 1}],
            "Cash", tendered,
            db_path=self.db_path, actor=CASHIER, **kwargs,
        )

    def _sale_count(self) -> int:
        conn = sqlite3.connect(self.db_path)
        try:
            return int(conn.execute(
                "SELECT COUNT(*) FROM store_sales"
            ).fetchone()[0])
        finally:
            conn.close()


class PosIntegrationTests(_DbCase):

    def test_preview_total_is_the_18_peso_boy_bawang(self) -> None:
        preview = store_sales.preview_sale(
            [{"barcode": BOY_BAWANG, "quantity": 1}], db_path=self.db_path
        )
        self.assertEqual(preview["total_amount"], PRICE)
        self.assertEqual(preview["items"][0]["product_name"],
                         "Boy Bawang Garlic 50 g")

    def test_live_preview_matches_the_committed_change(self) -> None:
        preview = change.compute(
            store_sales.preview_sale(
                [{"barcode": BOY_BAWANG, "quantity": 1}], db_path=self.db_path
            )["total_amount"],
            CASH,
        )
        result = self._sale(CASH)
        self.assertEqual(preview["change"], result["change_amount"])
        self.assertEqual(result["change_amount"], EXPECTED_CHANGE)
        self.assertEqual(result["amount_tendered"], 100.0)
        self.assertEqual(result["total_amount"], PRICE)

    def test_the_sale_is_atomic_and_deducts_one_item(self) -> None:
        before = self._stock()
        self._sale(CASH)
        self.assertEqual(self._stock(), before - 1)
        self.assertEqual(self._sale_count(), 1)
        conn = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM store_sale_items"
            ).fetchone()[0], 1)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM stock_out_transactions"
            ).fetchone()[0], 1)
        finally:
            conn.close()

    def test_receipt_shows_the_tendered_amount_and_change(self) -> None:
        result = self._sale(CASH)
        text = receipt.build_store_receipt(result)
        self.assertIn("Tendered", text)
        self.assertIn("Change", text)
        self.assertIn(format_money(100), text)
        self.assertIn(format_money(EXPECTED_CHANGE), text)
        for line in text.splitlines():
            self.assertLessEqual(len(line), receipt.RECEIPT_WIDTH, line)

    def test_insufficient_cash_writes_no_sale_and_no_stock_change(self) -> None:
        before_stock = self._stock()
        info = change.compute(PRICE, "10")
        self.assertFalse(info["ok"])
        with self.assertRaises(ValueError):
            self._sale("10")
        self.assertEqual(self._sale_count(), 0)
        self.assertEqual(self._stock(), before_stock)

    def test_discounted_sale_change_uses_the_net_total(self) -> None:
        result = self._sale(
            "100", discount_type=discounts.SENIOR,
            customer_name="Maria Santos", customer_id="SC-2026-00451",
        )
        self.assertEqual(result["subtotal_amount"], 18.0)
        self.assertEqual(result["discount_amount"], 3.60)
        self.assertEqual(result["total_amount"], 14.40)
        self.assertEqual(result["change_amount"], 85.60)
        self.assertEqual(change.compute(result["total_amount"], "100")["change"],
                         85.60)

    def test_exact_cash_produces_zero_change_in_the_database(self) -> None:
        result = self._sale("18")
        self.assertEqual(result["change_amount"], 0.0)
        self.assertEqual(change.compute(PRICE, "18")["status"],
                         change.STATUS_EXACT)

    def test_multiple_bills_can_still_be_typed_manually(self) -> None:
        # 100 + 20 handed over together: type 120, change is 102.
        result = self._sale("120")
        self.assertEqual(result["amount_tendered"], 120.0)
        self.assertEqual(result["change_amount"], 102.0)

    def test_confirm_sale_remains_the_authority_on_change(self) -> None:
        self.assertIn(
            "change = _money(tendered - total)", _source("store_sales.py")
        )


class WiringTests(unittest.TestCase):

    def setUp(self) -> None:
        self.pos = _source("pages/store_pos.py")

    def test_pos_imports_the_pure_change_module(self) -> None:
        self.assertIn("import change", self.pos)

    def test_tendered_entry_uses_one_shared_stringvar(self) -> None:
        self.assertIn("StringVar(master=self, value=\"\")", self.pos)
        self.assertIn("textvariable=self.tendered_var", self.pos)

    def test_a_write_trace_refreshes_the_change(self) -> None:
        self.assertIn("self.tendered_var.trace_add(", self.pos)
        self.assertIn("lambda *_args: self._render_change()", self.pos)

    def test_render_change_calls_the_pure_logic(self) -> None:
        self.assertIn(
            "change.compute(self.total_amount, self._tendered_text())", self.pos
        )

    def test_quick_cash_buttons_come_from_the_module(self) -> None:
        self.assertIn("change.QUICK_CASH", self.pos)
        self.assertIn("change.quick_cash_label(amount)", self.pos)
        self.assertIn("def _quick_cash(self, amount: int)", self.pos)

    def test_tendered_entry_has_no_placeholder(self) -> None:
        start = self.pos.index("self.tendered_entry = ctk.CTkEntry(")
        end = self.pos.index("self.tendered_entry.pack", start)
        self.assertNotIn("placeholder_text", self.pos[start:end])

    def test_both_change_labels_exist(self) -> None:
        self.assertIn("self.change_label = ctk.CTkLabel(", self.pos)
        self.assertIn("self.breakdown_label = ctk.CTkLabel(", self.pos)
        self.assertIn("self.breakdown_label.configure(", self.pos)

    def test_basket_recompute_refreshes_the_change(self) -> None:
        self.assertIn("def _render_cart(self)", self.pos)
        self.assertIn("self._render_change()", self.pos)

    def test_confirm_gate_keeps_its_old_meaning(self) -> None:
        self.assertIn("self._set_confirm_enabled(self.can_confirm)", self.pos)

    def test_post_sale_still_clears_the_tendered_field(self) -> None:
        self.assertIn("self.tendered_entry.delete(0, \"end\")", self.pos)

    def test_docstring_documents_stage_16(self) -> None:
        self.assertIn("Stage 16 - automatic change", self.pos)


class HygieneTests(unittest.TestCase):

    def test_change_module_is_pure(self) -> None:
        # Strip docstrings first: they legitimately mention SQL / Tk concepts.
        code = _code_without_strings(_source("change.py"))
        for forbidden in (
            "customtkinter", "sqlite3", "tkinter", "ctk",
            "INSERT INTO", "UPDATE ", "DELETE FROM", "BEGIN IMMEDIATE",
        ):
            self.assertNotIn(forbidden, code, forbidden)
        imported = {
            alias.name
            for node in ast.walk(ast.parse(_source("change.py")))
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        self.assertNotIn("sqlite3", imported)
        self.assertNotIn("customtkinter", imported)

    def test_change_module_uses_no_dynamic_execution(self) -> None:
        self.assertFalse(_has_dynamic_execution(_source("change.py")))

    def test_change_module_is_peso_only(self) -> None:
        source = _source("change.py")
        for forbidden in ("\u00a5", "\uffe5", "JPY", "CNY", "USD", "$"):
            self.assertNotIn(forbidden, source, forbidden)
        self.assertIn("from formatting import", source)

    def test_stage16_adds_no_third_party_dependency(self) -> None:
        requirements = _source("requirements.txt")
        for library in ("numpy", "pandas", "babel", "decimal", "money"):
            self.assertNotIn(library, requirements.lower())
        self.assertIn("customtkinter", requirements)
        self.assertIn("Pillow", requirements)

    def test_stage16_adds_no_schema_or_marker(self) -> None:
        self.assertNotIn("stage16_schema", _source("products.py"))
        self.assertNotIn("STAGE16", _source("products.py"))

    def test_user_version_is_still_4(self) -> None:
        temp = tempfile.TemporaryDirectory()
        try:
            path = Path(temp.name) / "v.db"
            database.initialize_database(path)
            info = products.ensure_schema(path)
            self.assertEqual(info["stage15_version"], "15")
            conn = sqlite3.connect(path)
            try:
                self.assertEqual(
                    conn.execute("PRAGMA user_version").fetchone()[0], 4
                )
            finally:
                conn.close()
        finally:
            temp.cleanup()

    def test_database_and_schema_are_not_rewritten(self) -> None:
        self.assertNotIn("change_amount REAL NOT NULL DEFAULT 0,",
                         _source("change.py"))
        self.assertIn("SCHEMA_VERSION = 4", _source("database.py"))

    def test_no_hardcoded_printer_or_device_names(self) -> None:
        self.assertNotIn("win32print", _source("change.py"))


if __name__ == "__main__":
    unittest.main()
