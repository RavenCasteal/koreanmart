"""
Stage 8 automated checks - final testing, debugging and polish.

Run from the project folder:
    python -m unittest discover -s tests -v

What these tests protect:
  * the ONE shared stock / expiry rule module (no duplicated definitions)
  * the shared number / unit formatting (fixes "5000 piece" -> "5000 pieces")
  * the final-build source hygiene (no placeholders, no stale stage labels)
  * the no-console launcher, crash log and Windows icon
  * that Stage 8 did NOT break the schema, the 8-page shell (Stage 9 added
    Point of Sale) or the strict READ-ONLY guarantees of the calculator and
    monitoring layers

Most tests use a temporary database. The real database/ramen_inventory.db is
opened read-only (mode=ro) and is never written by this file. No GUI is opened.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import re
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import patch

from PIL import Image

import database
import formatting
import main
import monitoring_repository as monitoring
import repository
import stock_rules
from components.shell import PAGES
from formatting import format_number, format_quantity, unit_label
from pages.dashboard import DashboardView

ROOT = Path(__file__).resolve().parents[1]
PAGES_DIR = ROOT / "pages"
TODAY = date(2026, 6, 15)

_WRITE_STARTS = (
    "insert", "update", "delete", "replace", "create", "drop", "alter",
)

# Any leftover f-string numeric format spec (:g, :+g, :.2f, ...) in a page.
_NUMERIC_FSTRING = re.compile(r":[+\- #0-9.]*[gfeG]\}")


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _imported_modules(relative: str) -> set[str]:
    tree = ast.parse(_source(relative))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def _write_sql_literals(relative: str) -> list[str]:
    """Find string literals that look like write SQL (AST, not prose scan)."""
    tree = ast.parse(_source(relative))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            first = node.value.strip().lower().split(" ", 1)[0]
            if first in _WRITE_STARTS:
                found.append(node.value.strip()[:60])
    return found


class Stage8SharedRulesTests(unittest.TestCase):
    """stock_rules.py is the single source of truth for status labels."""

    def test_stock_status_zero_is_out_of_stock(self) -> None:
        self.assertEqual(stock_rules.stock_status(0, 300), "Out of stock")

    def test_stock_status_negative_is_out_of_stock(self) -> None:
        self.assertEqual(stock_rules.stock_status(-1, 300), "Out of stock")

    def test_stock_status_at_minimum_is_low(self) -> None:
        self.assertEqual(stock_rules.stock_status(300, 300), "Low stock")

    def test_stock_status_below_minimum_is_low(self) -> None:
        self.assertEqual(stock_rules.stock_status(10, 300), "Low stock")

    def test_stock_status_above_minimum_is_in_stock(self) -> None:
        self.assertEqual(stock_rules.stock_status(301, 300), "In stock")

    def test_expiry_status_missing_date(self) -> None:
        self.assertEqual(stock_rules.expiry_status(None, TODAY), "Not set")
        self.assertEqual(stock_rules.expiry_status("", TODAY), "Not set")

    def test_expiry_status_invalid_date(self) -> None:
        for value in ("2026-99-99", "not-a-date", 12345):
            self.assertEqual(
                stock_rules.expiry_status(value, TODAY), "Invalid"
            )

    def test_expiry_status_expired(self) -> None:
        self.assertEqual(
            stock_rules.expiry_status("2026-06-14", TODAY), "Expired"
        )

    def test_expiry_status_today_is_near_expiry(self) -> None:
        self.assertEqual(
            stock_rules.expiry_status("2026-06-15", TODAY), "Near expiry"
        )

    def test_expiry_status_seventh_day_is_near_expiry(self) -> None:
        self.assertEqual(
            stock_rules.expiry_status("2026-06-22", TODAY), "Near expiry"
        )

    def test_expiry_status_eighth_day_is_ok(self) -> None:
        self.assertEqual(
            stock_rules.expiry_status("2026-06-23", TODAY), "OK"
        )

    def test_expiry_status_leap_day(self) -> None:
        self.assertEqual(
            stock_rules.expiry_status("2028-02-29", date(2028, 2, 22)),
            "Near expiry",
        )

    def test_repository_reexports_the_identical_functions(self) -> None:
        self.assertIs(repository.stock_status, stock_rules.stock_status)
        self.assertIs(repository.expiry_status, stock_rules.expiry_status)
        self.assertEqual(
            repository.EXPIRY_WARNING_DAYS,
            stock_rules.EXPIRY_WARNING_DAYS,
        )
        self.assertEqual(
            monitoring.EXPIRY_WARNING_DAYS,
            stock_rules.EXPIRY_WARNING_DAYS,
        )

    def test_dashboard_uses_the_shared_rule(self) -> None:
        self.assertIs(database.stock_status, stock_rules.stock_status)
        for quantity, minimum in ((0, 10), (5, 10), (10, 10), (11, 10)):
            self.assertEqual(
                database._ingredient_status(quantity, minimum),
                stock_rules.stock_status(quantity, minimum),
            )

    def test_pure_rule_modules_do_not_import_application_layers(self) -> None:
        self.assertTrue(
            _imported_modules("stock_rules.py")
            <= {"__future__", "datetime"}
        )
        self.assertTrue(
            _imported_modules("formatting.py")
            <= {"__future__", "math", "typing"}
        )


class Stage8FormattingTests(unittest.TestCase):
    """The Stage 8 pluralization bug fix and display helpers."""

    def test_the_original_bug_5000_piece_is_fixed(self) -> None:
        self.assertEqual(format_quantity(5000, "piece"), "5000 pieces")
        self.assertEqual(format_quantity(5000, "pieces"), "5000 pieces")

    def test_singular_forms(self) -> None:
        self.assertEqual(format_quantity(1, "gram"), "1 gram")
        self.assertEqual(format_quantity(1, "grams"), "1 gram")
        self.assertEqual(format_quantity(1, "piece"), "1 piece")
        self.assertEqual(format_quantity(1, "sheet"), "1 sheet")
        self.assertEqual(format_quantity(1, "egg"), "1 egg")
        self.assertEqual(format_quantity(1.0, "sheet"), "1 sheet")
        self.assertEqual(format_quantity(-1, "gram"), "-1 gram")

    def test_plural_forms(self) -> None:
        self.assertEqual(format_quantity(0, "piece"), "0 pieces")
        self.assertEqual(format_quantity(2, "gram"), "2 grams")
        self.assertEqual(format_quantity(2.5, "grams"), "2.5 grams")
        self.assertEqual(format_quantity(400, "sheet"), "400 sheets")

    def test_unknown_units_are_shown_verbatim(self) -> None:
        self.assertEqual(format_quantity(400, "ml"), "400 ml")
        self.assertEqual(format_quantity(1, "kg"), "1 kg")
        self.assertEqual(format_quantity(3, "handful"), "3 handful")

    def test_unit_label_is_case_insensitive_and_handles_empty(self) -> None:
        self.assertEqual(unit_label(1, "GRAM"), "gram")
        self.assertEqual(unit_label(2, "GRAM"), "grams")
        self.assertEqual(unit_label(1, ""), "")
        self.assertEqual(unit_label(1, None), "")

    def test_signed_quantities(self) -> None:
        self.assertEqual(
            format_quantity(250, "grams", signed=True), "+250 grams"
        )
        self.assertEqual(
            format_quantity(-250, "grams", signed=True), "-250 grams"
        )
        self.assertEqual(format_quantity(0, "grams", signed=True), "0 grams")

    def test_empty_unit_produces_a_bare_number(self) -> None:
        self.assertEqual(format_quantity(3, ""), "3")
        self.assertEqual(format_quantity(3), "3")

    def test_format_number_never_uses_scientific_notation(self) -> None:
        self.assertEqual(format_number(1_000_000), "1000000")
        self.assertEqual(format_number(1_000_000.0), "1000000")
        self.assertEqual(format_number(2.5), "2.5")
        self.assertEqual(format_number(0), "0")
        self.assertEqual(format_number(-0.0), "0")
        self.assertEqual(format_number(-12), "-12")

    def test_format_number_handles_string_and_bad_values(self) -> None:
        self.assertEqual(format_number("2"), "2")
        self.assertEqual(format_quantity("2", "piece"), "2 pieces")
        self.assertEqual(format_number(None), "None")
        self.assertEqual(format_number(float("inf")), "inf")
        self.assertEqual(format_number(float("nan")), "nan")


class Stage8SourceHygieneTests(unittest.TestCase):
    """No leftovers from earlier stages, and every page uses the formatter."""

    def test_no_raw_quantity_fstrings_left_in_pages(self) -> None:
        for path in sorted(PAGES_DIR.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(
                _NUMERIC_FSTRING.search(text),
                f"raw numeric f-string left in {path.name}",
            )

    def test_every_data_page_imports_the_shared_formatter(self) -> None:
        for name in (
            "dashboard.py", "ingredients.py", "recipe.py",
            "calculator.py", "inventory.py", "history.py", "monitoring.py",
            "pos.py",
        ):
            self.assertIn(
                "from formatting import",
                (PAGES_DIR / name).read_text(encoding="utf-8"),
                name,
            )

    def test_dead_placeholder_module_was_removed(self) -> None:
        self.assertFalse((PAGES_DIR / "placeholder.py").exists())
        for path in sorted(PAGES_DIR.glob("*.py")):
            self.assertNotIn(
                "Coming soon", path.read_text(encoding="utf-8"), path.name
            )

    def test_login_no_longer_says_stage_1(self) -> None:
        text = _source("pages/login.py")
        self.assertNotIn("College Capstone Project  ·  Stage 1", text)
        self.assertIn("Final build", text)

    def test_dashboard_rows_are_aligned_with_minsize_and_uniform(self) -> None:
        source = inspect.getsource(DashboardView._grid_row)
        self.assertIn("minsize=", source)
        self.assertIn("uniform=", source)

    def test_dashboard_surfaces_monitoring_alerts(self) -> None:
        text = _source("pages/dashboard.py")
        self.assertIn("monitoring_repository.get_alert_counts()", text)
        self.assertIn('on_navigate("monitoring")', text)
        self.assertIn("from formatting import format_quantity", text)

    def test_all_ten_pages_are_registered_and_refreshable(self) -> None:
        self.assertEqual(
            list(PAGES),
            [
                "dashboard", "ingredients", "products", "menu", "recipe",
                "calculator", "pos", "inventory", "history", "monitoring",
                "users",
            ],
        )
        for key, meta in PAGES.items():
            self.assertTrue(meta["label"], key)
            self.assertTrue(meta["title"], key)
            self.assertTrue(meta["subtitle"], key)
            self.assertTrue(callable(meta["view"].refresh), key)
            self.assertNotIn("Placeholder", meta["view"].__name__, key)

    def test_every_page_module_imports_cleanly(self) -> None:
        for path in sorted(PAGES_DIR.glob("*.py")):
            if path.name == "__init__.py":
                continue
            importlib.import_module(f"pages.{path.stem}")

    def test_read_only_layers_still_contain_no_write_sql(self) -> None:
        for relative in (
            "usage_repository.py", "monitoring_repository.py",
        ):
            self.assertEqual(_write_sql_literals(relative), [], relative)

    def test_launchers_are_windows_friendly(self) -> None:
        run = (ROOT / "run.bat").read_bytes()
        debug = (ROOT / "run_debug.bat").read_bytes()
        self.assertIn(b"pythonw.exe", run)
        self.assertIn(b"start \"\"", run)
        self.assertIn(b"python.exe", debug)
        for name, data in (("run.bat", run), ("run_debug.bat", debug)):
            self.assertEqual(data.count(b"\n"), data.count(b"\r\n"), name)

    def test_main_has_crash_logging_and_icon_support(self) -> None:
        self.assertTrue(callable(main.log_exception))
        self.assertTrue(callable(main._global_excepthook))
        self.assertTrue(callable(main.App.report_callback_exception))
        self.assertEqual(main.ERROR_LOG.name, "error.log")
        self.assertEqual(main.ICON_PATH.name, "ramen_icon.ico")
        self.assertIn("ERROR_LOG", _source("main.py"))

    def test_app_icon_is_a_valid_ico_file(self) -> None:
        path = ROOT / "assets" / "images" / "ramen_icon.ico"
        self.assertTrue(path.exists())
        self.assertGreater(path.stat().st_size, 1000)
        with Image.open(path) as image:
            self.assertEqual(image.format, "ICO")
            self.assertGreaterEqual(image.size[0], 16)
            image.load()

    def test_error_log_really_captures_a_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            log_path = Path(folder) / "logs" / "error.log"
            with patch.object(main, "ERROR_LOG", log_path):
                try:
                    raise RuntimeError("stage8 sentinel failure")
                except RuntimeError as exc:
                    main.log_exception("unit test", exc)
            text = log_path.read_text(encoding="utf-8")
            self.assertIn("unit test", text)
            self.assertIn("stage8 sentinel failure", text)
            self.assertIn("RuntimeError", text)


class Stage8DatabaseTests(unittest.TestCase):
    """Stage 8 changes must not alter data or break existing behaviour."""

    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.db_path = Path(temp_dir.name) / "ramen_inventory.db"
        database.initialize_database(self.db_path)
        self._healthy()

    def _healthy(self) -> None:
        with database.transaction(self.db_path) as conn:
            conn.execute(
                """
                UPDATE ingredients
                SET quantity_on_hand = 1000, minimum_stock = 10,
                    expiration_date = NULL, is_active = 1
                """
            )

    def _set(self, name: str, **changes: Any) -> None:
        columns = {
            "balance": "quantity_on_hand",
            "minimum": "minimum_stock",
            "expiry": "expiration_date",
            "active": "is_active",
        }
        assignments = [f"{columns[k]} = ?" for k in changes]
        params = list(changes.values()) + [name]
        with database.transaction(self.db_path) as conn:
            conn.execute(
                "UPDATE ingredients SET "
                + ", ".join(assignments)
                + " WHERE name = ?",
                params,
            )

    def _hash(self) -> str:
        return hashlib.sha256(self.db_path.read_bytes()).hexdigest()

    def _counts(self) -> dict[str, Any]:
        return monitoring.get_alert_counts(today=TODAY, db_path=self.db_path)

    def test_alert_counts_are_all_zero_for_a_healthy_catalog(self) -> None:
        counts = self._counts()
        self.assertEqual(counts["total_active"], 8)
        self.assertEqual(counts["needs_attention"], 0)
        self.assertEqual(counts["critical"], 0)
        self.assertEqual(counts["healthy"], 8)
        self.assertEqual(counts["warning_days"], 7)

    def test_alert_counts_classify_every_alert_type(self) -> None:
        self._set("Ramen noodles", balance=0)
        self._set("Pork", balance=5)
        self._set("Ramen broth", expiry="2026-06-14")
        self._set("Egg", expiry="2026-06-16")
        self._set("Nori sheets", expiry="2026-99-99")
        counts = self._counts()
        self.assertEqual(counts["out_of_stock"], 1)
        self.assertEqual(counts["low_stock"], 1)
        self.assertEqual(counts["expired"], 1)
        self.assertEqual(counts["near_expiry"], 1)
        self.assertEqual(counts["invalid_expiry"], 1)
        self.assertEqual(counts["needs_attention"], 5)
        self.assertEqual(counts["critical"], 2)
        self.assertEqual(counts["stock_alerts"], 2)
        self.assertEqual(counts["expiry_alerts"], 2)

    def test_soft_deleted_ingredients_are_excluded_from_alert_counts(self) -> None:
        self._set("Garlic", balance=0, active=0)
        counts = self._counts()
        self.assertEqual(counts["total_active"], 7)
        self.assertEqual(counts["out_of_stock"], 0)

    def test_dashboard_and_monitoring_agree_on_stock_counts(self) -> None:
        self._set("Ramen noodles", balance=0)
        self._set("Pork", balance=0)
        self._set("Egg", balance=5)
        self._set("Garlic", balance=10)
        data = database.get_dashboard_data(self.db_path)
        counts = self._counts()
        self.assertEqual(data["out_of_stock"], 2)
        self.assertEqual(data["low_stock"], 2)
        self.assertEqual(data["out_of_stock"], counts["out_of_stock"])
        self.assertEqual(data["low_stock"], counts["low_stock"])

    def test_get_alert_counts_does_not_modify_the_database(self) -> None:
        before = self._hash()
        for _ in range(3):
            self._counts()
        self.assertEqual(self._hash(), before)

    def test_get_alert_counts_works_on_a_read_only_connection(self) -> None:
        def read_only(_path: Any) -> sqlite3.Connection:
            return sqlite3.connect(
                f"file:{self.db_path}?mode=ro", uri=True
            )

        with patch("database.get_connection", side_effect=read_only):
            counts = self._counts()
        self.assertEqual(counts["total_active"], 8)

    def test_schema_has_all_stage9_tables_and_integrity_is_clean(self) -> None:
        conn = database.get_connection(self.db_path)
        try:
            tables = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            self.assertEqual(
                tables,
                {
                    "users", "menu_items", "recipes", "recipe_ingredients",
                    "ingredients", "stock_in_transactions",
                    "stock_in_details", "stock_out_transactions",
                    "stock_out_details", "sales_transactions",
                    "sale_deductions",
                },
            )
            self.assertEqual(
                conn.execute("PRAGMA integrity_check").fetchone()[0], "ok"
            )
            self.assertEqual(
                list(conn.execute("PRAGMA foreign_key_check")), []
            )
        finally:
            conn.close()

    def test_seeded_single_menu_item_and_eight_recipe_lines(self) -> None:
        conn = database.get_connection(self.db_path)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM menu_items").fetchone()[0], 1
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM recipe_ingredients")
                .fetchone()[0], 8
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM ingredients").fetchone()[0], 8
            )
        finally:
            conn.close()

    def test_passwords_are_hashed_not_plaintext(self) -> None:
        conn = database.get_connection(self.db_path)
        try:
            hashes = [
                row[0]
                for row in conn.execute("SELECT password_hash FROM users")
            ]
        finally:
            conn.close()
        self.assertTrue(hashes)
        for value in hashes:
            self.assertTrue(value.startswith("pbkdf2_sha256$"))
            self.assertNotEqual(value, database.DEFAULT_ADMIN_PASSWORD)
            self.assertNotIn(database.DEFAULT_ADMIN_PASSWORD, value)


class Stage8RealDatabaseTests(unittest.TestCase):
    """Read-only sanity check on the delivered database file."""

    def test_real_database_is_intact_and_read_only_access_works(self) -> None:
        path = ROOT / "database" / "ramen_inventory.db"
        self.assertTrue(path.exists())
        # as_uri() escapes the space in "WorkBuddy AI" for SQLite's URI mode.
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        try:
            self.assertEqual(
                conn.execute("PRAGMA integrity_check").fetchone()[0], "ok"
            )
            self.assertEqual(
                list(conn.execute("PRAGMA foreign_key_check")), []
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
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
