"""
Stage 15 automated checks - Senior Citizen / PWD discount + cashier calculator.

VERIFIED BEHAVIOURS
  1. discounts.py    - 20% whole-sale rule, labels, one-discount-only,
                       required name/ID, legacy-sale normalisation
  2. calculator.py   - four-function state machine, no eval(), Error recovery,
                       every keypad key used by the POS
  3. products.py     - six additive store_sales columns, idempotent upgrade,
                       app_meta marker, v4 PRAGMA user_version untouched
  4. store_sales.py  - discount computed INSIDE BEGIN IMMEDIATE from freshly
                       resolved prices, immutable snapshots, atomic rejection,
                       net tender/change, void reversal, gross = pre-discount
  5. receipt.py      - Subtotal / discount / net TOTAL / customer details,
                       58 mm width, peso-only, legacy receipts still valid
  6. sales_report.py - daily_summary, Z-read and CSV expose discounts
  7. UI wiring       - POS controls, embedded keypad, post-sale reset,
                       Reports sixth KPI, source hygiene

Every transactional test uses a FRESH temporary SQLite database. The delivered
database/ramen_inventory.db is never opened for writing here.
"""

from __future__ import annotations

import ast
import csv
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import calculator
import database
import discounts
import permissions
import products
import receipt
import sales_report
import store_sales
from formatting import CURRENCY_CODE, CURRENCY_SYMBOL, format_money

ROOT = Path(__file__).resolve().parents[1]
MANAGER: dict[str, Any] = {"user_id": 1, "username": "admin", "role": "manager"}
CASHIER: dict[str, Any] = {"user_id": 2, "username": "cashier", "role": "cashier"}

COKE = "4800001234567"
WATER = "4800002234564"
CART: list[dict[str, Any]] = [
    {"barcode": COKE, "quantity": 2},
    {"barcode": WATER, "quantity": 1},
]
# 2 x 35.00 + 1 x 15.00 = 85.00; 20% = 17.00; net = 68.00
SUBTOTAL = 85.0
DISCOUNT = 17.0
NET = 68.0

NAME = "Maria Santos"
ID_NUMBER = "SC-2026-00451"

# The exact Stage 12 store_sales shape, used to prove a delivered database
# upgrades in place (CREATE TABLE IF NOT EXISTS + ALTER TABLE).
_LEGACY_SALES_DDL = """
CREATE TABLE store_sales (
    sale_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_number  TEXT    NOT NULL UNIQUE,
    transaction_date    TEXT    NOT NULL,
    item_count          INTEGER NOT NULL CHECK (item_count > 0),
    total_amount        REAL    NOT NULL CHECK (total_amount >= 0),
    currency_code       TEXT    NOT NULL DEFAULT 'PHP',
    payment_method      TEXT    NOT NULL,
    amount_tendered     REAL    NOT NULL,
    change_amount       REAL    NOT NULL DEFAULT 0,
    status              TEXT    NOT NULL DEFAULT 'completed',
    stock_out_id        INTEGER,
    created_by          INTEGER,
    voided_at           TEXT,
    voided_by           INTEGER,
    void_reason         TEXT
)
"""


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _has_dynamic_execution(source: str) -> bool:
    """Check real CALL nodes, not the word 'eval' in a docstring."""
    tree = ast.parse(source)
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in ("eval", "exec", "compile")
        for node in ast.walk(tree)
    )


class _Stage15Case(unittest.TestCase):
    def setUp(self) -> None:
        permissions.clear_session()
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "stage15.db"
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
                "stock_out_details", "stock_in_transactions", "stock_in_details",
            )
        }

    def _sale(
        self,
        cart: Any = None,
        tendered: Any = "100",
        actor: Any = MANAGER,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return store_sales.confirm_sale(
            CART if cart is None else cart,
            "Cash", tendered,
            user_id=1, db_path=self.db_path, actor=actor, **kwargs,
        )


# ======================================================================
# 1 - Pure discount rules
# ======================================================================
class DiscountRuleTests(unittest.TestCase):

    def test_rate_is_exactly_twenty_percent(self) -> None:
        self.assertEqual(discounts.DISCOUNT_RATE, 0.20)
        self.assertEqual(discounts.NO_DISCOUNT, "")
        self.assertEqual(discounts.SENIOR, "senior")
        self.assertEqual(discounts.PWD, "pwd")

    def test_choices_are_the_three_ui_labels(self) -> None:
        self.assertEqual(
            discounts.CHOICES, ("No discount", "Senior Citizen", "PWD")
        )
        self.assertEqual(len(set(discounts.CHOICES)), 3)

    def test_normalize_accepts_codes_and_ui_labels(self) -> None:
        for value, expected in (
            ("", discounts.NO_DISCOUNT),
            (None, discounts.NO_DISCOUNT),
            ("none", discounts.NO_DISCOUNT),
            ("No discount", discounts.NO_DISCOUNT),
            ("senior", discounts.SENIOR),
            ("Senior Citizen", discounts.SENIOR),
            ("SC", discounts.SENIOR),
            ("pwd", discounts.PWD),
            ("PWD", discounts.PWD),
            ("  Persons with disability  ", discounts.PWD),
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    discounts.normalize_discount_type(value), expected
                )

    def test_normalize_rejects_an_unknown_discount(self) -> None:
        for value in ("vip", "employee", "senior and pwd", "50%"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    discounts.normalize_discount_type(value)

    def test_two_discounts_cannot_be_stacked(self) -> None:
        # "Senior + PWD" is not a valid selection: exactly ONE per sale.
        with self.assertRaises(ValueError):
            discounts.compute(SUBTOTAL, "senior,pwd")
        with self.assertRaises(ValueError):
            discounts.compute(SUBTOTAL, "senior pwd")

    def test_compute_without_a_discount(self) -> None:
        result = discounts.compute(SUBTOTAL, discounts.NO_DISCOUNT)
        self.assertEqual(result["subtotal_amount"], SUBTOTAL)
        self.assertEqual(result["discount_rate"], 0.0)
        self.assertEqual(result["discount_amount"], 0.0)
        self.assertEqual(result["total_amount"], SUBTOTAL)
        self.assertEqual(result["discount_type"], discounts.NO_DISCOUNT)
        self.assertEqual(result["discount_label"], "No discount")

    def test_senior_and_pwd_both_take_twenty_percent_of_the_whole_sale(self) -> None:
        for kind in (discounts.SENIOR, discounts.PWD):
            with self.subTest(kind=kind):
                result = discounts.compute(SUBTOTAL, kind)
                self.assertEqual(result["subtotal_amount"], SUBTOTAL)
                self.assertEqual(result["discount_rate"], 0.20)
                self.assertEqual(result["discount_amount"], DISCOUNT)
                self.assertEqual(result["total_amount"], NET)
                self.assertEqual(result["discount_type"], kind)

    def test_discount_is_on_the_whole_basket_not_per_item(self) -> None:
        # 20% of (a + b) must equal the single whole-sale discount.
        whole = discounts.compute(70.0 + 15.0, discounts.SENIOR)
        separate = discounts.compute(70.0, discounts.SENIOR)["total_amount"]
        separate += discounts.compute(15.0, discounts.SENIOR)["total_amount"]
        self.assertEqual(whole["total_amount"], round(separate, 2))

    def test_compute_rounds_to_centavos(self) -> None:
        result = discounts.compute(33.33, discounts.SENIOR)
        self.assertEqual(result["discount_amount"], 6.67)
        self.assertEqual(result["total_amount"], 26.66)
        self.assertAlmostEqual(
            result["subtotal_amount"] - result["discount_amount"],
            result["total_amount"], places=2,
        )

    def test_zero_and_negative_never_produce_a_negative_total(self) -> None:
        result = discounts.compute(0.0, discounts.PWD)
        self.assertEqual(result["total_amount"], 0.0)
        self.assertEqual(result["discount_amount"], 0.0)

    def test_money_matches_the_sale_money_helper(self) -> None:
        for value in (0.005, 1.005, 33.335, 99.999, 1234.565):
            with self.subTest(value=value):
                self.assertEqual(
                    discounts.money(value), store_sales._money(value)
                )

    def test_labels_and_short_labels(self) -> None:
        self.assertEqual(discounts.label(discounts.SENIOR), "Senior Citizen")
        self.assertEqual(discounts.label(discounts.PWD), "PWD")
        self.assertEqual(discounts.label(discounts.NO_DISCOUNT), "No discount")
        self.assertEqual(discounts.short_label(discounts.SENIOR), "Senior 20%")
        self.assertEqual(discounts.short_label(discounts.PWD), "PWD 20%")
        self.assertEqual(discounts.short_label(discounts.NO_DISCOUNT), "")

    def test_no_discount_ignores_stray_customer_details(self) -> None:
        name, number = discounts.validate_customer(
            "  stray  name ", " 999 ", discounts.NO_DISCOUNT
        )
        self.assertEqual((name, number), ("", ""))

    def test_validate_customer_normalises_whitespace(self) -> None:
        name, number = discounts.validate_customer(
            "  Maria   Santos  ", " SC-2026-00451 ", discounts.SENIOR
        )
        self.assertEqual(name, "Maria Santos")
        self.assertEqual(number, "SC-2026-00451")

    def test_discount_requires_a_name(self) -> None:
        for name in ("", " ", "A", None):
            with self.subTest(name=name):
                with self.assertRaises(ValueError) as ctx:
                    discounts.validate_customer(
                        name, ID_NUMBER, discounts.SENIOR
                    )
                self.assertIn("name", str(ctx.exception).lower())

    def test_discount_requires_an_id_number(self) -> None:
        for number in ("", " ", "1", "AB", None):
            with self.subTest(number=number):
                with self.assertRaises(ValueError) as ctx:
                    discounts.validate_customer(NAME, number, discounts.PWD)
                self.assertIn("id", str(ctx.exception).lower())

    def test_discount_rejects_overlong_name_and_id(self) -> None:
        with self.assertRaises(ValueError):
            discounts.validate_customer("A" * 81, ID_NUMBER, discounts.SENIOR)
        with self.assertRaises(ValueError):
            discounts.validate_customer(NAME, "X" * 41, discounts.PWD)

    def test_apply_to_sale_leaves_a_legacy_sale_unchanged_in_value(self) -> None:
        legacy = {"total_amount": 82.0, "amount_tendered": 100.0}
        filled = discounts.apply_to_sale(legacy)
        self.assertEqual(filled["subtotal_amount"], 82.0)
        self.assertEqual(filled["discount_amount"], 0.0)
        self.assertEqual(filled["discount_type"], discounts.NO_DISCOUNT)
        self.assertEqual(filled["customer_name"], "")
        self.assertEqual(legacy, {"total_amount": 82.0, "amount_tendered": 100.0})

    def test_apply_to_sale_preserves_a_discounted_snapshot(self) -> None:
        sale = discounts.compute(SUBTOTAL, discounts.SENIOR)
        sale.update({"total_amount": NET, "customer_name": NAME,
                     "customer_id": ID_NUMBER})
        filled = discounts.apply_to_sale(sale)
        self.assertEqual(filled["subtotal_amount"], SUBTOTAL)
        self.assertEqual(filled["discount_amount"], DISCOUNT)
        self.assertEqual(filled["discount_label"], "Senior Citizen")
        self.assertEqual(filled["customer_name"], NAME)

    def test_apply_to_sale_handles_a_legacy_zero_subtotal(self) -> None:
        filled = discounts.apply_to_sale(
            {"subtotal_amount": 0.0, "total_amount": 50.0}
        )
        self.assertEqual(filled["subtotal_amount"], 50.0)

    def test_discount_module_is_pure(self) -> None:
        source = _source("discounts.py")
        for forbidden in ("import tkinter", "customtkinter", "sqlite3",
                          "SELECT ", "INSERT "):
            self.assertNotIn(forbidden, source)
        self.assertFalse(_has_dynamic_execution(source))


# ======================================================================
# 2 - Cashier calculator
# ======================================================================
class CalculatorTests(unittest.TestCase):

    def setUp(self) -> None:
        self.calc = calculator.Calculator()

    def _press(self, *keys: str) -> str:
        text = "0"
        for key in keys:
            text = self.calc.press(key)
        return text

    def test_starts_at_zero(self) -> None:
        self.assertEqual(self.calc.display, "0")
        self.assertEqual(self.calc.value, 0.0)
        self.assertFalse(self.calc.error)

    def test_multiplication(self) -> None:
        self.assertEqual(self._press("3", "x", "5", "="), "15")
        self.assertEqual(self.calc.value, 15.0)

    def test_addition_subtraction_division(self) -> None:
        self.assertEqual(self._press("8", "+", "2", "="), "10")
        self.assertEqual(self._press("C", "9", "-", "4", "="), "5")
        self.assertEqual(self._press("C", "9", "/", "2", "="), "4.5")

    def test_chained_operations(self) -> None:
        self.assertEqual(self._press("2", "+", "3", "+", "4", "="), "9")
        self.assertEqual(self._press("C", "5", "x", "2", "x", "3", "="), "30")

    def test_keyboard_aliases_match_the_physical_keypad(self) -> None:
        self.assertEqual(self._press("3", "X", "5", "="), "15")
        self.assertEqual(self._press("C", "3", "\u00d7", "5", "="), "15")
        self.assertEqual(self._press("C", "8", "\u00f7", "2", "="), "4")

    def test_every_key_on_the_embedded_keypad_is_accepted(self) -> None:
        keypad = ("C", "Del", "/", "x", "7", "8", "9", "-", "4", "5",
                  "6", "+", "1", "2", "3", "=", "00", "0", ".", "%")
        for key in keypad:
            with self.subTest(key=key):
                self.calc.clear()
                self.assertIsInstance(self.calc.press(key), str)

    def test_double_zero_key_builds_a_hundred(self) -> None:
        self.assertEqual(self._press("1"), "1")
        self.calc.press("0")
        self.calc.press("0")
        self.assertEqual(self.calc.display, "100")

    def test_decimal_point_is_only_added_once(self) -> None:
        self.assertEqual(self._press(".", "5", ".", "5"), "0.55")

    def test_backspace_alias_del(self) -> None:
        self._press("1", "2", "3")
        self.assertEqual(self.calc.press("Del"), "12")
        self.assertEqual(self.calc.press("del"), "1")
        self.assertEqual(self.calc.press("\u232b"), "0")

    def test_clear_resets_all_state(self) -> None:
        self._press("9", "+", "4")
        self.assertEqual(self.calc.press("C"), "0")
        self.assertEqual(self.calc.press("="), "0")

    def test_negate(self) -> None:
        self._press("5")
        self.assertEqual(self.calc.press("+/-"), "-5")
        self.assertEqual(self.calc.press("+/-"), "5")

    def test_percent(self) -> None:
        self._press("5", "0")
        self.assertEqual(self.calc.press("%"), "0.5")

    def test_divide_by_zero_is_a_recoverable_error(self) -> None:
        self._press("5", "/", "0", "=")
        self.assertEqual(self.calc.display, calculator.ERROR_TEXT)
        self.assertTrue(self.calc.error)
        self.assertEqual(self.calc.value, 0.0)
        self.assertEqual(self.calc.press("7"), "7")
        self.assertFalse(self.calc.error)

    def test_clear_recovers_from_error(self) -> None:
        self._press("5", "/", "0", "=")
        self.assertEqual(self.calc.press("C"), "0")
        self.assertFalse(self.calc.error)

    def test_digit_limit(self) -> None:
        self._press(*("1" for _ in range(20)))
        self.assertLessEqual(len(self.calc.display.replace("-", "")),
                             calculator.MAX_DIGITS + 8)

    def test_unknown_keys_are_rejected(self) -> None:
        for key in ("a", "@", "sqrt", "(", "1+2"):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    self.calc.press(key)

    def test_calculator_never_uses_eval(self) -> None:
        source = _source("calculator.py")
        self.assertFalse(_has_dynamic_execution(source))
        self.assertNotIn("import tkinter", source)
        self.assertNotIn("sqlite3", source)

    def test_a_calculator_result_is_a_valid_tendered_amount(self) -> None:
        self._press("1", "0", "0", "-", "3", "2", "=")
        result = self.calc.value
        self.assertEqual(result, 68.0)
        # This is the amount the "Use result as amount tendered" button sends.
        self.assertEqual(store_sales.validate_tendered(str(result)), 68.0)


# ======================================================================
# 3 - Additive schema
# ======================================================================
class Stage15SchemaTests(unittest.TestCase):

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "schema15.db"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _columns(self, table: str) -> set[str]:
        conn = sqlite3.connect(self.db_path)
        try:
            return {str(row[1]) for row in conn.execute(
                f"PRAGMA table_info({table})"
            )}
        finally:
            conn.close()

    def test_a_fresh_database_gets_all_six_columns(self) -> None:
        database.initialize_database(self.db_path)
        report = products.ensure_schema(self.db_path)
        self.assertEqual(
            set(report["added_sale_columns"]),
            {"subtotal_amount", "discount_type", "discount_rate",
             "discount_amount", "customer_name", "customer_id"},
        )
        self.assertEqual(report["stage15_version"], "15")
        self.assertTrue(
            {"subtotal_amount", "discount_type", "discount_rate",
             "discount_amount", "customer_name", "customer_id"}
            <= self._columns("store_sales")
        )

    def test_ensure_schema_is_idempotent(self) -> None:
        database.initialize_database(self.db_path)
        products.ensure_schema(self.db_path)
        second = products.ensure_schema(self.db_path)
        self.assertEqual(second["added_sale_columns"], [])
        self.assertEqual(second["added_columns"], [])

    def test_stage12_database_upgrades_in_place_with_safe_defaults(self) -> None:
        database.initialize_database(self.db_path)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(_LEGACY_SALES_DDL)
            conn.execute(
                "INSERT INTO store_sales (transaction_number, transaction_date, "
                "item_count, total_amount, payment_method, amount_tendered) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("SM-20261101-0001", "2026-11-01 09:00:00", 1, 100.0,
                 "Cash", 100.0),
            )
            conn.commit()
        finally:
            conn.close()

        before = self._columns("store_sales")
        self.assertNotIn("discount_amount", before)
        report = products.ensure_schema(self.db_path)
        self.assertEqual(len(report["added_sale_columns"]), 6)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = dict(conn.execute(
                "SELECT * FROM store_sales WHERE transaction_number = ?",
                ("SM-20261101-0001",),
            ).fetchone())
        finally:
            conn.close()
        self.assertEqual(row["total_amount"], 100.0)
        self.assertEqual(row["subtotal_amount"], 0.0)
        self.assertEqual(row["discount_amount"], 0.0)
        self.assertEqual(row["discount_type"], "")
        self.assertEqual(row["discount_rate"], 0.0)
        self.assertEqual(row["customer_name"], "")
        self.assertEqual(row["customer_id"], "")

    def test_app_meta_marker_is_written_without_bumping_user_version(self) -> None:
        database.initialize_database(self.db_path)
        products.ensure_schema(self.db_path)
        conn = sqlite3.connect(self.db_path)
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            marker = conn.execute(
                "SELECT value FROM app_meta WHERE key = 'stage15_schema'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(version, 4)
        self.assertEqual(marker, "15")

    def test_new_columns_have_check_constraints_and_defaults(self) -> None:
        database.initialize_database(self.db_path)
        products.ensure_schema(self.db_path)
        conn = sqlite3.connect(self.db_path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO store_sales (transaction_number, "
                    "transaction_date, item_count, total_amount, "
                    "payment_method, amount_tendered, discount_amount) "
                    "VALUES ('X1', '2026-11-13 10:00:00', 1, 10, "
                    "'Cash', 10, -1)"
                )
        finally:
            conn.close()

    def test_database_py_and_schema_sql_were_not_changed(self) -> None:
        self.assertEqual(database.SCHEMA_VERSION, 4)
        self.assertNotIn("stage15", _source("database.py").lower())
        self.assertNotIn("stage15", _source("database/schema.sql").lower())
        self.assertNotIn("discount_amount", _source("database/schema.sql").lower())


# ======================================================================
# 4 - Atomic discounted sale
# ======================================================================
class DiscountedSaleTests(_Stage15Case):

    def test_preview_is_read_only_and_returns_the_discount_breakdown(self) -> None:
        before = self._counts()
        preview = store_sales.preview_sale(
            CART, db_path=self.db_path, discount_type=discounts.SENIOR
        )
        self.assertEqual(preview["subtotal_amount"], SUBTOTAL)
        self.assertEqual(preview["discount_amount"], DISCOUNT)
        self.assertEqual(preview["total_amount"], NET)
        self.assertEqual(preview["discount_rate"], 0.20)
        self.assertTrue(preview["can_confirm"])
        self.assertEqual(self._counts(), before)

    def test_preview_defaults_to_no_discount(self) -> None:
        preview = store_sales.preview_sale(CART, db_path=self.db_path)
        self.assertEqual(preview["total_amount"], SUBTOTAL)
        self.assertEqual(preview["discount_amount"], 0.0)

    def test_preview_accepts_the_ui_label(self) -> None:
        preview = store_sales.preview_sale(
            CART, db_path=self.db_path, discount_type="Senior Citizen"
        )
        self.assertEqual(preview["total_amount"], NET)

    def test_preview_rejects_an_unknown_discount(self) -> None:
        with self.assertRaises(ValueError):
            store_sales.preview_sale(
                CART, db_path=self.db_path, discount_type="vip"
            )

    def test_confirm_writes_an_immutable_discount_snapshot(self) -> None:
        result = self._sale(
            discount_type=discounts.SENIOR,
            customer_name=NAME, customer_id=ID_NUMBER,
        )
        self.assertEqual(result["subtotal_amount"], SUBTOTAL)
        self.assertEqual(result["discount_amount"], DISCOUNT)
        self.assertEqual(result["total_amount"], NET)
        self.assertEqual(result["change_amount"], 32.0)
        self.assertEqual(result["discount_label"], "Senior Citizen")
        self.assertEqual(result["customer_name"], NAME)
        self.assertEqual(result["customer_id"], ID_NUMBER)

        row = self._query("SELECT * FROM store_sales")[0]
        for key in ("subtotal_amount", "discount_type", "discount_rate",
                    "discount_amount", "total_amount", "customer_name",
                    "customer_id"):
            self.assertEqual(row[key], result[key], key)

    def test_pwd_sale_is_recorded_with_its_own_type(self) -> None:
        result = self._sale(
            discount_type="PWD", customer_name=NAME, customer_id=ID_NUMBER
        )
        row = self._query("SELECT * FROM store_sales")[0]
        self.assertEqual(row["discount_type"], discounts.PWD)
        self.assertEqual(result["discount_label"], "PWD")
        self.assertEqual(row["discount_amount"], DISCOUNT)

    def test_a_plain_sale_has_no_customer_details_even_if_stray_text(self) -> None:
        result = self._sale(
            customer_name="stray", customer_id="999999"
        )
        row = self._query("SELECT * FROM store_sales")[0]
        self.assertEqual(row["discount_type"], discounts.NO_DISCOUNT)
        self.assertEqual(row["discount_amount"], 0.0)
        self.assertEqual(row["subtotal_amount"], SUBTOTAL)
        self.assertEqual(row["total_amount"], SUBTOTAL)
        self.assertEqual(row["customer_name"], "")
        self.assertEqual(row["customer_id"], "")
        self.assertEqual(result["total_amount"], SUBTOTAL)

    def test_missing_name_writes_nothing(self) -> None:
        before = self._counts()
        with self.assertRaises(ValueError) as ctx:
            self._sale(
                discount_type=discounts.SENIOR,
                customer_name="", customer_id=ID_NUMBER,
            )
        self.assertIn("name", str(ctx.exception).lower())
        self.assertEqual(self._counts(), before)

    def test_missing_id_writes_nothing(self) -> None:
        before = self._counts()
        with self.assertRaises(ValueError) as ctx:
            self._sale(
                discount_type=discounts.PWD,
                customer_name=NAME, customer_id="",
            )
        self.assertIn("id", str(ctx.exception).lower())
        self.assertEqual(self._counts(), before)

    def test_invalid_discount_type_writes_nothing(self) -> None:
        before = self._counts()
        with self.assertRaises(ValueError):
            self._sale(
                discount_type="vip", customer_name=NAME, customer_id=ID_NUMBER
            )
        self.assertEqual(self._counts(), before)

    def test_tender_is_checked_against_the_net_total_not_the_subtotal(self) -> None:
        # 70.00 is LESS than the 85.00 subtotal but MORE than the 68.00 net.
        result = self._sale(
            tendered="70",
            discount_type=discounts.SENIOR,
            customer_name=NAME, customer_id=ID_NUMBER,
        )
        self.assertEqual(result["total_amount"], NET)
        self.assertEqual(result["change_amount"], 2.0)

    def test_tender_below_the_net_total_is_rejected_atomically(self) -> None:
        before = self._counts()
        with self.assertRaises(ValueError):
            self._sale(
                tendered="67",
                discount_type=discounts.SENIOR,
                customer_name=NAME, customer_id=ID_NUMBER,
            )
        self.assertEqual(self._counts(), before)

    def test_discount_never_changes_inventory_deduction(self) -> None:
        before = self._query(
            "SELECT quantity_on_hand FROM ingredients WHERE barcode = ?", (COKE,)
        )[0]["quantity_on_hand"]
        self._sale(
            discount_type=discounts.SENIOR,
            customer_name=NAME, customer_id=ID_NUMBER,
        )
        after = self._query(
            "SELECT quantity_on_hand FROM ingredients WHERE barcode = ?", (COKE,)
        )[0]["quantity_on_hand"]
        self.assertEqual(before - after, 2)
        lines = self._query("SELECT * FROM store_sale_items")
        self.assertEqual(sum(float(r["line_total"]) for r in lines), SUBTOTAL)

    def test_discount_is_recomputed_inside_the_transaction(self) -> None:
        # Reprice AFTER preview: the committed discount must use the NEW price.
        preview = store_sales.preview_sale(
            CART, db_path=self.db_path, discount_type=discounts.SENIOR
        )
        self.assertEqual(preview["discount_amount"], DISCOUNT)
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE ingredients SET unit_price = 70.00 WHERE barcode = ?",
                (COKE,),
            )
            conn.commit()
        finally:
            conn.close()
        # 2 x 70 + 15 = 155; 20% = 31; net = 124. Tender must cover the net.
        result = self._sale(
            tendered="200",
            discount_type=discounts.SENIOR,
            customer_name=NAME, customer_id=ID_NUMBER,
        )
        self.assertEqual(result["subtotal_amount"], 155.0)
        self.assertEqual(result["discount_amount"], 31.0)
        self.assertEqual(result["total_amount"], 124.0)

    def test_shortages_still_block_a_discounted_sale(self) -> None:
        before = self._counts()
        with self.assertRaises(ValueError):
            self._sale(
                cart=[{"barcode": COKE, "quantity": 9999}],
                tendered="100000",
                discount_type=discounts.PWD,
                customer_name=NAME, customer_id=ID_NUMBER,
            )
        self.assertEqual(self._counts(), before)

    def test_permission_guard_still_applies_to_discounted_sales(self) -> None:
        # Both real roles may record a sale, so prove the guard is invoked by
        # making it reject. Nothing may be written when it does.
        before = self._counts()
        denial = permissions.PermissionDenied("Blocked for this test.")
        with patch.object(permissions, "require", side_effect=denial):
            with self.assertRaises(permissions.PermissionDenied):
                self._sale(
                    discount_type=discounts.SENIOR,
                    customer_name=NAME, customer_id=ID_NUMBER,
                )
        self.assertEqual(self._counts(), before)

    def test_cashier_can_record_a_discounted_sale(self) -> None:
        result = self._sale(
            actor=CASHIER,
            discount_type=discounts.SENIOR,
            customer_name=NAME, customer_id=ID_NUMBER,
        )
        self.assertEqual(result["total_amount"], NET)

    def test_gross_sales_is_pre_discount(self) -> None:
        self._sale(
            discount_type=discounts.SENIOR,
            customer_name=NAME, customer_id=ID_NUMBER,
        )
        self.assertEqual(store_sales.gross_sales(self.db_path), SUBTOTAL)

    def test_gross_sales_mixes_legacy_and_discounted_rows(self) -> None:
        self._sale()  # legacy-shaped row, subtotal 85, total 85
        self._sale(
            discount_type=discounts.PWD,
            customer_name=NAME, customer_id=ID_NUMBER,
        )
        self.assertEqual(store_sales.gross_sales(self.db_path), 170.0)

    def test_get_sale_detail_exposes_discount_fields_for_reprint(self) -> None:
        result = self._sale(
            discount_type=discounts.SENIOR,
            customer_name=NAME, customer_id=ID_NUMBER,
        )
        detail = store_sales.get_sale_detail(result["sale_id"], self.db_path)
        detail["items"] = list(detail["items"])
        for key in ("subtotal_amount", "discount_type", "discount_rate",
                    "discount_amount", "customer_name", "customer_id"):
            self.assertIn(key, detail)
        self.assertEqual(detail["discount_amount"], DISCOUNT)
        text = receipt.build_store_receipt(detail, reprint=True)
        self.assertIn("REPRINT COPY", text)
        self.assertIn(NAME, text)

    def test_void_reverses_a_discounted_sale_and_restores_stock(self) -> None:
        before = self._query(
            "SELECT quantity_on_hand FROM ingredients WHERE barcode = ?", (COKE,)
        )[0]["quantity_on_hand"]
        result = self._sale(
            discount_type=discounts.SENIOR,
            customer_name=NAME, customer_id=ID_NUMBER,
        )
        store_sales.void_sale(
            result["sale_id"], "Customer returned the items",
            user_id=1, db_path=self.db_path, actor=MANAGER,
        )
        after = self._query(
            "SELECT quantity_on_hand FROM ingredients WHERE barcode = ?", (COKE,)
        )[0]["quantity_on_hand"]
        self.assertEqual(before, after)
        self.assertEqual(store_sales.gross_sales(self.db_path), 0.0)
        row = self._query("SELECT * FROM store_sales")[0]
        self.assertEqual(row["status"], "voided")
        # The immutable discount snapshot is NEVER deleted or zeroed.
        self.assertEqual(row["discount_amount"], DISCOUNT)
        self.assertEqual(row["customer_name"], NAME)

    def test_two_discounted_sales_have_distinct_transaction_numbers(self) -> None:
        first = self._sale(
            discount_type=discounts.SENIOR,
            customer_name=NAME, customer_id=ID_NUMBER,
        )
        second = self._sale(
            discount_type=discounts.PWD,
            customer_name="Juan Cruz", customer_id="PWD-7788",
        )
        self.assertNotEqual(
            first["transaction_number"], second["transaction_number"]
        )
        self.assertEqual(len(self._query("SELECT * FROM store_sales")), 2)


# ======================================================================
# 5 - Receipt (58 mm / 32 columns / peso only)
# ======================================================================
class DiscountedReceiptTests(unittest.TestCase):

    def setUp(self) -> None:
        self.sale: dict[str, Any] = {
            "transaction_number": "SM-20261113-0001",
            "transaction_date": "2026-11-13 10:00:00",
            "cashier": "cashier",
            "items": [
                {"product_name": "Coca-Cola Original 500 ml", "barcode": COKE,
                 "category": "Beverages", "quantity": 2, "unit_price": 35.0,
                 "line_total": 70.0},
                {"product_name": "Pure Drinking Water 500 ml",
                 "barcode": WATER, "category": "Beverages", "quantity": 1,
                 "unit_price": 15.0, "line_total": 15.0},
            ],
            "status": "completed",
            "payment_method": "Cash",
            "currency_code": CURRENCY_CODE,
            **discounts.compute(SUBTOTAL, discounts.SENIOR),
            "amount_tendered": 100.0,
            "change_amount": 32.0,
            "customer_name": NAME,
            "customer_id": ID_NUMBER,
        }

    def test_receipt_shows_subtotal_discount_and_net_total(self) -> None:
        text = receipt.build_store_receipt(self.sale)
        self.assertIn("Subtotal", text)
        self.assertIn(format_money(SUBTOTAL), text)
        self.assertIn("Senior 20%", text)
        self.assertIn("-" + format_money(DISCOUNT), text)
        self.assertIn("TOTAL", text)
        self.assertIn(format_money(NET), text)
        self.assertIn(format_money(32.0), text)

    def test_receipt_prints_proof_of_eligibility(self) -> None:
        text = receipt.build_store_receipt(self.sale)
        self.assertIn("DISCOUNT DETAILS", text)
        self.assertIn(NAME, text)
        self.assertIn("ID no.", text)
        self.assertIn(ID_NUMBER, text)
        self.assertIn("Senior Citizen", text)

    def test_pwd_receipt_uses_the_pwd_label(self) -> None:
        sale = dict(self.sale)
        sale.update(discounts.compute(SUBTOTAL, discounts.PWD))
        sale["customer_name"] = NAME
        sale["customer_id"] = ID_NUMBER
        text = receipt.build_store_receipt(sale)
        self.assertIn("PWD 20%", text)
        self.assertIn("Discount type", text)
        self.assertNotIn("Senior 20%", text)

    def test_no_discount_receipt_has_no_discount_details_block(self) -> None:
        sale = dict(self.sale)
        sale.update(discounts.compute(SUBTOTAL, discounts.NO_DISCOUNT))
        sale["amount_tendered"] = 100.0
        sale["change_amount"] = 15.0
        text = receipt.build_store_receipt(sale)
        self.assertIn("Subtotal", text)
        self.assertNotIn("DISCOUNT DETAILS", text)
        self.assertNotIn("Senior 20%", text)
        self.assertIn(format_money(SUBTOTAL), text)

    def test_legacy_sale_still_builds_a_valid_receipt(self) -> None:
        legacy = {
            "transaction_number": "SM-20261101-0001",
            "transaction_date": "2026-11-01 09:00:00",
            "cashier": "admin",
            "items": self.sale["items"],
            "total_amount": 85.0,
            "amount_tendered": 100.0,
            "change_amount": 15.0,
            "payment_method": "Cash",
            "currency_code": CURRENCY_CODE,
            "status": "completed",
        }
        text = receipt.build_store_receipt(legacy)
        self.assertIn("Subtotal", text)
        self.assertIn(format_money(85.0), text)
        self.assertNotIn("DISCOUNT DETAILS", text)

    def test_every_receipt_line_fits_58_mm_paper(self) -> None:
        for kind in (discounts.NO_DISCOUNT, discounts.SENIOR, discounts.PWD):
            sale = dict(self.sale)
            sale.update(discounts.compute(SUBTOTAL, kind))
            sale["customer_name"] = NAME
            sale["customer_id"] = ID_NUMBER
            text = receipt.build_store_receipt(sale)
            with self.subTest(kind=kind):
                self.assertLessEqual(
                    max(len(line) for line in text.splitlines()),
                    receipt.RECEIPT_WIDTH,
                )

    def test_receipt_uses_peso_only(self) -> None:
        text = receipt.build_store_receipt(self.sale)
        self.assertIn(CURRENCY_SYMBOL, text)
        self.assertIn(CURRENCY_CODE, text)
        self.assertNotIn("\u00a5", text)
        self.assertNotIn("\uffe5", text)
        self.assertNotIn("JPY", text)
        self.assertNotIn("CNY", text)

    def test_escpos_bytes_still_build_for_a_discounted_receipt(self) -> None:
        text = receipt.build_store_receipt(self.sale)
        payload = receipt.build_escpos_bytes(text)
        self.assertIsInstance(payload, bytes)
        self.assertIn(b"Subtotal", payload)
        self.assertTrue(payload.startswith(b"\x1b@"))
        self.assertTrue(payload.endswith(b"\x1dVB\x00"))

    def test_voided_discounted_receipt_keeps_the_snapshot(self) -> None:
        sale = dict(self.sale)
        sale["status"] = "voided"
        sale["voided_at"] = "2026-11-13 11:00:00"
        sale["void_reason"] = "Wrong customer"
        text = receipt.build_store_receipt(sale)
        self.assertIn("VOIDED", text)
        self.assertIn("DISCOUNT DETAILS", text)
        self.assertIn(NAME, text)


# ======================================================================
# 6 - Z-read + CSV reporting
# ======================================================================
class DiscountReportingTests(_Stage15Case):

    def _summary(self) -> dict[str, Any]:
        return sales_report.daily_summary(db_path=self.db_path, actor=MANAGER)

    def test_empty_day_has_zero_discounts(self) -> None:
        summary = self._summary()
        self.assertEqual(summary["discount_amount"], 0.0)
        self.assertEqual(summary["discount_count"], 0)
        self.assertEqual(summary["net_sales"], 0.0)

    def test_plain_sale_reports_as_before_stage15(self) -> None:
        self._sale()
        summary = self._summary()
        self.assertEqual(summary["gross_sales"], SUBTOTAL)
        self.assertEqual(summary["voided_amount"], 0.0)
        self.assertEqual(summary["discount_amount"], 0.0)
        self.assertEqual(summary["net_sales"], SUBTOTAL)
        self.assertEqual(summary["discount_count"], 0)

    def test_discounted_sale_reduces_net_sales(self) -> None:
        self._sale(
            discount_type=discounts.SENIOR,
            customer_name=NAME, customer_id=ID_NUMBER,
        )
        summary = self._summary()
        self.assertEqual(summary["gross_sales"], SUBTOTAL)
        self.assertEqual(summary["discount_amount"], DISCOUNT)
        self.assertEqual(summary["net_sales"], NET)
        self.assertEqual(summary["discount_count"], 1)
        self.assertEqual(
            summary["discounts_by_type"][discounts.SENIOR]["count"], 1
        )
        self.assertEqual(
            summary["discounts_by_type"][discounts.SENIOR]["total"], DISCOUNT
        )
        self.assertEqual(
            summary["discounts_by_type"][discounts.PWD]["count"], 0
        )

    def test_discount_breakdown_separates_senior_and_pwd(self) -> None:
        self._sale(
            discount_type=discounts.SENIOR,
            customer_name=NAME, customer_id=ID_NUMBER,
        )
        self._sale(
            discount_type=discounts.PWD,
            customer_name="Juan Cruz", customer_id="PWD-7788",
        )
        summary = self._summary()
        self.assertEqual(summary["discount_count"], 2)
        self.assertEqual(summary["discount_amount"], 34.0)
        self.assertEqual(summary["gross_sales"], 170.0)
        self.assertEqual(summary["net_sales"], 136.0)
        for kind in (discounts.SENIOR, discounts.PWD):
            self.assertEqual(summary["discounts_by_type"][kind]["count"], 1)

    def test_voided_discount_does_not_count_as_granted(self) -> None:
        result = self._sale(
            discount_type=discounts.SENIOR,
            customer_name=NAME, customer_id=ID_NUMBER,
        )
        store_sales.void_sale(
            result["sale_id"], "Cancelled",
            user_id=1, db_path=self.db_path, actor=MANAGER,
        )
        summary = self._summary()
        self.assertEqual(summary["gross_sales"], SUBTOTAL)
        self.assertEqual(summary["voided_amount"], SUBTOTAL)
        self.assertEqual(summary["discount_amount"], 0.0)
        self.assertEqual(summary["discount_count"], 0)
        self.assertEqual(summary["net_sales"], 0.0)

    def test_transaction_rows_expose_discount_detail(self) -> None:
        self._sale(
            discount_type=discounts.PWD,
            customer_name=NAME, customer_id=ID_NUMBER,
        )
        row = self._summary()["transactions"][0]
        self.assertEqual(row["subtotal_amount"], SUBTOTAL)
        self.assertEqual(row["total_amount"], NET)
        self.assertEqual(row["discount_amount"], DISCOUNT)
        self.assertEqual(row["discount_type"], discounts.PWD)
        self.assertEqual(row["discount_label"], "PWD")
        self.assertEqual(row["customer_name"], NAME)
        self.assertEqual(row["customer_id"], ID_NUMBER)

    def test_legacy_sale_row_falls_back_to_total_as_subtotal(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO store_sales (transaction_number, transaction_date, "
                "item_count, total_amount, payment_method, amount_tendered) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("SM-LEGACY-0001", database.local_timestamp(), 1, 50.0,
                 "Cash", 50.0),
            )
            conn.commit()
        finally:
            conn.close()
        summary = self._summary()
        self.assertEqual(summary["gross_sales"], 50.0)
        self.assertEqual(summary["net_sales"], 50.0)
        self.assertEqual(summary["discount_amount"], 0.0)
        self.assertEqual(summary["transactions"][0]["subtotal_amount"], 50.0)

    def test_z_read_shows_discounts_and_keeps_payment_mix(self) -> None:
        self._sale(
            discount_type=discounts.SENIOR,
            customer_name=NAME, customer_id=ID_NUMBER,
        )
        text = sales_report.build_z_read(self._summary())
        self.assertIn("SC/PWD discounts", text)
        self.assertIn("-" + format_money(DISCOUNT), text)
        self.assertIn("SENIOR / PWD DISCOUNTS", text)
        self.assertIn("Senior Citizen", text)
        self.assertIn("PAYMENT MIX", text)
        self.assertIn("NET SALES", text)
        self.assertIn(format_money(NET), text)

    def test_z_read_on_a_day_with_no_discounts(self) -> None:
        text = sales_report.build_z_read(self._summary())
        self.assertIn("(no discounted sales)", text)
        self.assertIn("SC/PWD discounts", text)

    def test_csv_keeps_the_original_eight_columns_and_appends_five(self) -> None:
        self._sale(
            discount_type=discounts.SENIOR,
            customer_name=NAME, customer_id=ID_NUMBER,
        )
        target = sales_report.export_transactions_csv(
            db_path=self.db_path, base_dir=self.temp.name, actor=MANAGER
        )
        with target.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(len(rows[0]), 13)
        self.assertEqual(rows[0][:8], [
            "Transaction no.", "Date / time", "Cashier", "Items",
            f"Total ({CURRENCY_CODE})", "Payment", "Status", "Void reason",
        ])
        self.assertEqual(rows[0][8:], [
            f"Subtotal ({CURRENCY_CODE})", "Discount type",
            f"Discount ({CURRENCY_CODE})", "Customer name", "ID number",
        ])
        self.assertEqual(rows[1][4], f"{NET:.2f}")
        self.assertEqual(rows[1][8], f"{SUBTOTAL:.2f}")
        self.assertEqual(rows[1][9], "Senior Citizen")
        self.assertEqual(rows[1][10], f"{DISCOUNT:.2f}")
        self.assertEqual(rows[1][11], NAME)
        self.assertEqual(rows[1][12], ID_NUMBER)

    def test_csv_labels_a_plain_sale_as_no_discount(self) -> None:
        self._sale()
        target = sales_report.export_transactions_csv(
            db_path=self.db_path, base_dir=self.temp.name, actor=MANAGER
        )
        with target.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(rows[1][9], "No discount")
        self.assertEqual(rows[1][10], "0.00")
        self.assertEqual(rows[1][11], "-")
        self.assertEqual(rows[1][12], "-")

    def test_z_read_can_be_archived_without_printing(self) -> None:
        self._sale(
            discount_type=discounts.PWD,
            customer_name=NAME, customer_id=ID_NUMBER,
        )
        outcome = sales_report.issue_z_read(
            self._summary(), auto_print=False,
            base_dir=self.temp.name, actor=MANAGER,
        )
        self.assertTrue(outcome["path"].exists())
        self.assertIn("SC/PWD discounts", outcome["text"])
        self.assertEqual(outcome["print"]["method"], "skipped")

    def test_reports_are_manager_only(self) -> None:
        with self.assertRaises(permissions.PermissionDenied):
            sales_report.daily_summary(db_path=self.db_path, actor={"role": "cashier"})


# ======================================================================
# 7 - UI wiring + source hygiene
# ======================================================================
class Stage15SourceHygieneTests(unittest.TestCase):

    def test_pos_has_the_discount_controls(self) -> None:
        source = _source("pages/store_pos.py")
        for marker in (
            "import discounts", "import calculator",
            "self.discount_combo", "self.customer_name_entry",
            "self.customer_id_entry", "self.discount_status_label",
            "20% off the whole sale",
        ):
            self.assertIn(marker, source)

    def test_pos_has_the_embedded_calculator(self) -> None:
        source = _source("pages/store_pos.py")
        for marker in (
            "Cashier calculator", "self.calc_display", "self.calc = ",
            "self._calc_press", "self._use_calc_result",
            "Use result as amount tendered",
        ):
            self.assertIn(marker, source)
        self.assertIn('("C", "Del", "/", "x")', source)

    def test_confirm_passes_the_discount_and_customer_fields(self) -> None:
        source = _source("pages/store_pos.py")
        start = source.index("def _confirm(self)")
        end = source.index("def _sale_record", start)
        body = source[start:end]
        for marker in (
            "discount_type=self._discount_type()",
            "customer_name=self.customer_name_entry.get()",
            "customer_id=self.customer_id_entry.get()",
        ):
            self.assertIn(marker, body)

    def test_a_discount_cannot_leak_to_the_next_customer(self) -> None:
        source = _source("pages/store_pos.py")
        start = source.index("def _confirm(self)")
        end = source.index("def _sale_record", start)
        body = source[start:end]
        self.assertIn("self._reset_discount()", body)
        self.assertIn("self.calc.clear()", body)
        self.assertIn("self.calc_display.configure", body)
        self.assertIn("def _reset_discount", source)
        self.assertIn("self.discount_combo.set", source)
        self.assertIn('def _clear(self)', source)
        clear_body = source[source.index("def _clear(self)"):
                            source.index("def _use_exact_total", source.index("def _clear(self)"))]
        self.assertIn("self._reset_discount()", clear_body)

    def test_confirm_is_blocked_until_customer_details_are_entered(self) -> None:
        source = _source("pages/store_pos.py")
        start = source.index("def _render_cart(self)")
        end = source.index("for index, item in enumerate(self.cart)", start)
        body = source[start:end]
        self.assertIn("details_ready", body)
        self.assertIn("preview_sale(self.cart, discount_type=kind)", body)
        self.assertIn("self.can_confirm", body)

    def test_reports_page_has_six_kpis_including_discounts(self) -> None:
        source = _source("pages/reports.py")
        self.assertIn("for column in range(6)", source)
        self.assertIn('"Discounts"', source)
        self.assertIn("Before discounts", source)
        self.assertIn("After discounts", source)

    def test_stage15_adds_no_third_party_dependency(self) -> None:
        source = _source("requirements.txt").lower()
        self.assertNotIn("numpy", source)
        self.assertNotIn("pandas", source)
        self.assertNotIn("decimal", source)
        self.assertFalse(_has_dynamic_execution(_source("discounts.py")))
        self.assertFalse(_has_dynamic_execution(_source("calculator.py")))

    def test_pos_still_uses_the_shared_peso_formatter(self) -> None:
        source = _source("pages/store_pos.py")
        self.assertIn("format_money", source)
        self.assertIn("from formatting import", source)

    def test_scanner_and_printer_paths_are_untouched(self) -> None:
        source = _source("pages/store_pos.py")
        for marker in (
            "def _on_root_key", "def _install_scanner_focus",
            "def _teardown_scanner_focus", "def _add_by_barcode",
            "self._confirming", "issue_store_receipt",
        ):
            self.assertIn(marker, source)


if __name__ == "__main__":
    unittest.main()
