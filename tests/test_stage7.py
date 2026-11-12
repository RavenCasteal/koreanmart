"""
Stage 7 automated checks - low-stock and expiration monitoring.

Run from the project folder:
    python -m unittest discover -s tests -v

Uses a temporary database; the real database/ramen_inventory.db is untouched.
No GUI is opened. The single most important guarantee here is that monitoring
is READ-ONLY: it never edits a balance, never changes an expiry date and never
records a stock transaction.
"""

from __future__ import annotations

import ast
import hashlib
import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import database
import monitoring_repository as monitoring
import repository
import stock_repository as stock

# A fixed reference date makes every expiry classification deterministic.
TODAY = date(2026, 6, 15)
TODAY_ISO = TODAY.isoformat()

# ----------------------------------------------------------------------
# Deterministic fixture: all eight seeded ingredients are given a known
# balance / minimum / expiry so every status, severity and filter can be
# asserted exactly. "2026-99-99" passes the schema's digit-only GLOB check
# but is not a real calendar date, so it exercises the "Invalid" branch.
# ----------------------------------------------------------------------
FIXTURE: dict[str, dict[str, Any]] = {
    "Ramen noodles": {"balance": 0, "minimum": 300, "expiry": None},
    "Pork": {"balance": 100, "minimum": 200, "expiry": "2026-06-17"},
    "Ramen broth": {"balance": 1000, "minimum": 800, "expiry": "2026-06-14"},
    "Egg": {"balance": 10, "minimum": 2, "expiry": "2026-07-20"},
    "Nori sheets": {"balance": 3, "minimum": 2, "expiry": "2026-99-99"},
    "Green onions": {"balance": 20, "minimum": 20, "expiry": None},
    "Soy sauce": {"balance": 31, "minimum": 30, "expiry": TODAY_ISO},
    "Garlic": {"balance": 100, "minimum": 10, "expiry": "2026-06-22"},
}


class Stage7MonitoringTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.db_path = Path(temp_dir.name) / "ramen_inventory.db"
        database.initialize_database(self.db_path)

    # ------------------------------------------------------------------
    # Helpers (fixture setup only - the module under test is read-only)
    # ------------------------------------------------------------------
    def _raw(self, sql: str, params: tuple = ()) -> list[tuple]:
        with database.transaction(self.db_path) as conn:
            return [tuple(row) for row in conn.execute(sql, params).fetchall()]

    def _hash(self) -> str:
        return hashlib.sha256(self.db_path.read_bytes()).hexdigest()

    def _set(self, name: str, **changes: Any) -> None:
        """Set catalog/balance columns directly for a precise test fixture."""
        columns = {
            "balance": "quantity_on_hand",
            "minimum": "minimum_stock",
            "expiry": "expiration_date",
            "active": "is_active",
        }
        assignments: list[str] = []
        params: list[Any] = []
        for key, column in columns.items():
            if key in changes:
                assignments.append(f"{column} = ?")
                params.append(changes[key])
        if not assignments:
            return
        params.append(name)
        with database.transaction(self.db_path) as conn:
            conn.execute(
                f"UPDATE ingredients SET {', '.join(assignments)} WHERE name = ?",
                params,
            )

    def _fixture(self) -> None:
        for name, state in FIXTURE.items():
            self._set(name, **state)
        # A soft-deleted ingredient must never appear on the watchlist.
        repository.add_ingredient(
            "Old stock", "grams", 0, None, db_path=self.db_path
        )
        self._set("Old stock", balance=0, active=0)

    def _data(
        self,
        search: str = "",
        alert_filter: str = monitoring.FILTER_ALL,
        today: Any = TODAY,
    ) -> dict[str, Any]:
        return monitoring.get_monitoring_data(
            search, alert_filter, today=today, db_path=self.db_path
        )

    def _row(self, name: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        result = data if data is not None else self._data()
        return next(row for row in result["all_rows"] if row["name"] == name)

    # ------------------------------------------------------------------
    # Shared classification helpers
    # ------------------------------------------------------------------
    def test_resolve_today_accepts_none_date_datetime_and_iso(self) -> None:
        self.assertEqual(monitoring.resolve_today(), date.today())
        self.assertEqual(monitoring.resolve_today(TODAY), TODAY)
        self.assertEqual(
            monitoring.resolve_today(datetime(2026, 6, 15, 23, 45)), TODAY
        )
        self.assertEqual(monitoring.resolve_today("2026-06-15"), TODAY)
        self.assertEqual(monitoring.resolve_today("  2026-06-15  "), TODAY)

    def test_resolve_today_rejects_malformed_dates(self) -> None:
        for value in ("nope", "2026-13-01", "15/06/2026", 12345):
            with self.subTest(value=value), self.assertRaises(ValueError):
                monitoring.resolve_today(value)

    def test_stock_status_boundaries(self) -> None:
        self.assertEqual(repository.stock_status(0, 5), "Out of stock")
        self.assertEqual(repository.stock_status(-1, 5), "Out of stock")
        self.assertEqual(repository.stock_status(5, 5), "Low stock")
        self.assertEqual(repository.stock_status(2.5, 5), "Low stock")
        self.assertEqual(repository.stock_status(5.01, 5), "In stock")
        self.assertEqual(repository.stock_status(1, 0), "In stock")

    def test_expiry_status_boundaries(self) -> None:
        cases = (
            ("2026-06-14", "Expired"),       # 1 day ago
            (TODAY_ISO, "Near expiry"),      # today is included
            ("2026-06-21", "Near expiry"),   # 6 days
            ("2026-06-22", "Near expiry"),   # exactly 7 days
            ("2026-06-23", "OK"),            # 8 days is outside the window
            ("2026-07-20", "OK"),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(
                    repository.expiry_status(value, TODAY), expected
                )

    def test_expiry_warning_window_is_seven_days(self) -> None:
        self.assertEqual(repository.EXPIRY_WARNING_DAYS, 7)
        self.assertEqual(self._data()["warning_days"], 7)
        self.assertEqual(self._data()["summary"]["warning_days"], 7)

    def test_expiry_status_handles_leap_day(self) -> None:
        self.assertEqual(
            repository.expiry_status("2028-02-29", date(2028, 2, 25)),
            "Near expiry",
        )
        self.assertEqual(
            repository.expiry_status("2028-02-29", date(2028, 2, 22)),
            "Near expiry",  # exactly 7 days, across the leap day
        )
        self.assertEqual(
            repository.expiry_status("2028-02-29", date(2028, 2, 20)),
            "OK",            # 9 days away
        )

    def test_expiry_status_invalid_and_missing_dates(self) -> None:
        self.assertEqual(repository.expiry_status(None, TODAY), "Not set")
        self.assertEqual(repository.expiry_status("", TODAY), "Not set")
        self.assertEqual(repository.expiry_status("bad", TODAY), "Invalid")
        self.assertEqual(repository.expiry_status("2026-99-99", TODAY), "Invalid")

    # ------------------------------------------------------------------
    # Seeded state (zero stock, no expiry dates)
    # ------------------------------------------------------------------
    def test_seeded_state_has_eight_active_ingredients(self) -> None:
        data = self._data()
        self.assertEqual(data["total"], 8)
        self.assertEqual(data["shown"], 8)
        self.assertEqual(data["today"], TODAY_ISO)
        self.assertEqual(data["filter"], monitoring.FILTER_ALL)

    def test_seeded_zero_stock_makes_every_row_critical(self) -> None:
        data = self._data()
        self.assertEqual(
            {row["status"] for row in data["rows"]}, {"Out of stock"}
        )
        self.assertEqual(
            {row["severity"] for row in data["rows"]},
            {monitoring.SEVERITY_CRITICAL},
        )
        self.assertTrue(all(row["needs_attention"] for row in data["rows"]))
        self.assertEqual(data["summary"]["out_of_stock"], 8)
        self.assertEqual(data["summary"]["critical"], 8)
        self.assertEqual(data["summary"]["healthy"], 0)
        self.assertEqual(data["summary"]["needs_attention"], 8)

    def test_seeded_missing_expiry_is_not_an_alert(self) -> None:
        row = self._row("Ramen noodles")
        self.assertEqual(row["expiry_status"], "Not set")
        self.assertEqual(row["expiry_text"], "Not set")
        self.assertIsNone(row["days_left"])
        self.assertEqual(row["alert_reasons"], ["Out of stock"])

    # ------------------------------------------------------------------
    # Classification, severity, sorting
    # ------------------------------------------------------------------
    def test_fixture_classifies_each_ingredient(self) -> None:
        self._fixture()
        data = self._data()
        expected = {
            "Ramen noodles": ("Out of stock", "Not set"),
            "Pork": ("Low stock", "Near expiry"),
            "Ramen broth": ("In stock", "Expired"),
            "Egg": ("In stock", "OK"),
            "Nori sheets": ("In stock", "Invalid"),
            "Green onions": ("Low stock", "Not set"),
            "Soy sauce": ("In stock", "Near expiry"),
            "Garlic": ("In stock", "Near expiry"),
        }
        for name, (status, expiry) in expected.items():
            with self.subTest(name=name):
                row = self._row(name, data)
                self.assertEqual(row["status"], status)
                self.assertEqual(row["expiry_status"], expiry)
        self.assertEqual(data["total"], 8)  # soft-deleted row excluded

    def test_fixture_severity_levels(self) -> None:
        self._fixture()
        data = self._data()
        expected = {
            "Ramen noodles": monitoring.SEVERITY_CRITICAL,   # out of stock
            "Ramen broth": monitoring.SEVERITY_CRITICAL,     # expired
            "Pork": monitoring.SEVERITY_WARNING,             # low + near
            "Soy sauce": monitoring.SEVERITY_WARNING,        # near (today)
            "Garlic": monitoring.SEVERITY_WARNING,           # near (7 days)
            "Green onions": monitoring.SEVERITY_WARNING,     # low stock
            "Nori sheets": monitoring.SEVERITY_WARNING,      # invalid date
            "Egg": monitoring.SEVERITY_HEALTHY,
        }
        for name, severity in expected.items():
            with self.subTest(name=name):
                self.assertEqual(self._row(name, data)["severity"], severity)

    def test_fixture_summary_counts(self) -> None:
        self._fixture()
        summary = self._data()["summary"]
        self.assertEqual(summary["total_active"], 8)
        self.assertEqual(summary["out_of_stock"], 1)
        self.assertEqual(summary["low_stock"], 2)
        self.assertEqual(summary["near_expiry"], 3)
        self.assertEqual(summary["expired"], 1)
        self.assertEqual(summary["invalid_expiry"], 1)
        self.assertEqual(summary["needs_attention"], 7)
        self.assertEqual(summary["healthy"], 1)
        self.assertEqual(summary["critical"], 2)
        self.assertEqual(summary["stock_alerts"], 3)
        self.assertEqual(summary["expiry_alerts"], 4)

    def test_fixture_expiry_text_and_days_left(self) -> None:
        self._fixture()
        data = self._data()
        cases = (
            ("Ramen broth", "2026-06-14 · 1 d ago", -1),
            ("Soy sauce", "2026-06-15 · today", 0),
            ("Pork", "2026-06-17 · in 2 d", 2),
            ("Garlic", "2026-06-22 · in 7 d", 7),
            ("Egg", "2026-07-20", 35),
            ("Nori sheets", "Invalid date", None),
            ("Ramen noodles", "Not set", None),
        )
        for name, text, days in cases:
            with self.subTest(name=name):
                row = self._row(name, data)
                self.assertEqual(row["expiry_text"], text)
                self.assertEqual(row["days_left"], days)

    def test_rows_sorted_by_severity_then_expiry_then_name(self) -> None:
        self._fixture()
        names = [row["name"] for row in self._data()["rows"]]
        self.assertEqual(
            names,
            [
                "Ramen broth",    # Critical, expired (-1 d)
                "Ramen noodles",  # Critical, no expiry
                "Soy sauce",      # Warning, today (0 d)
                "Pork",           # Warning, 2 d
                "Garlic",         # Warning, 7 d
                "Green onions",   # Warning, no expiry (alphabetical)
                "Nori sheets",    # Warning, invalid (alphabetical)
                "Egg",            # Healthy
            ],
        )

    def test_all_rows_sorted_by_name(self) -> None:
        self._fixture()
        names = [row["name"] for row in self._data()["all_rows"]]
        self.assertEqual(names, sorted(names, key=str.lower))

    def test_alert_reasons_and_needs_attention(self) -> None:
        self._fixture()
        data = self._data()
        self.assertEqual(
            self._row("Pork", data)["alert_reasons"],
            ["Low stock", "Near expiry"],
        )
        self.assertEqual(
            self._row("Nori sheets", data)["alert_reasons"],
            ["Invalid expiry date"],
        )
        self.assertEqual(self._row("Egg", data)["alert_reasons"], [])
        self.assertFalse(self._row("Egg", data)["needs_attention"])
        self.assertTrue(self._row("Pork", data)["needs_attention"])

    # ------------------------------------------------------------------
    # Filters
    # ------------------------------------------------------------------
    def test_every_filter_returns_the_expected_rows(self) -> None:
        self._fixture()
        expected = {
            monitoring.FILTER_ALL: 8,
            monitoring.FILTER_ATTENTION: 7,
            monitoring.FILTER_STOCK: 3,
            monitoring.FILTER_EXPIRY: 5,
            monitoring.FILTER_CRITICAL: 2,
        }
        for code, count in expected.items():
            with self.subTest(code=code):
                data = self._data("", code)
                self.assertEqual(data["filter"], code)
                self.assertEqual(data["shown"], count)
                self.assertEqual(data["total"], 8)

    def test_filter_labels_and_codes_both_work(self) -> None:
        self._fixture()
        for label, code in monitoring.FILTER_CODES.items():
            with self.subTest(label=label):
                by_label = [row["name"] for row in self._data("", label)["rows"]]
                by_code = [row["name"] for row in self._data("", code)["rows"]]
                self.assertEqual(by_label, by_code)

    def test_unknown_filter_is_rejected(self) -> None:
        for value in ("NOPE", "Everything", "", 42):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self._data("", str(value))

    def test_filter_and_search_combine(self) -> None:
        self._fixture()
        self.assertEqual(self._data("pork", "EXPIRY")["shown"], 1)
        self.assertEqual(self._data("pork", "STOCK")["shown"], 1)
        self.assertEqual(self._data("pork", "CRITICAL")["shown"], 0)
        self.assertEqual(self._data("egg", "ATTENTION")["shown"], 0)
        self.assertEqual(self._data("egg", "ALL")["shown"], 1)

    def test_stock_and_expiry_alert_row_lists(self) -> None:
        self._fixture()
        data = self._data()
        self.assertEqual(len(data["stock_alert_rows"]), 3)
        # The expiry list also contains invalid dates, while the summary's
        # expiry_alerts deliberately counts only near-expiry + expired.
        self.assertEqual(len(data["expiry_alert_rows"]), 5)
        self.assertEqual(
            data["summary"]["expiry_alerts"]
            + data["summary"]["invalid_expiry"],
            len(data["expiry_alert_rows"]),
        )

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------
    def test_search_matches_name_case_insensitively_and_trims(self) -> None:
        self._fixture()
        for keyword in ("pork", "PORK", "  Pork  "):
            with self.subTest(keyword=keyword):
                rows = self._data(keyword)["rows"]
                self.assertEqual([row["name"] for row in rows], ["Pork"])

    def test_search_matches_unit(self) -> None:
        self._fixture()
        names = {row["name"] for row in self._data("grams")["rows"]}
        self.assertEqual(
            names,
            {"Ramen noodles", "Pork", "Green onions", "Garlic"},
        )

    def test_search_matches_status_and_expiry_status(self) -> None:
        self._fixture()
        low = {row["name"] for row in self._data("low stock")["rows"]}
        self.assertEqual(low, {"Pork", "Green onions"})
        near = {row["name"] for row in self._data("near expiry")["rows"]}
        self.assertEqual(near, {"Pork", "Soy sauce", "Garlic"})

    def test_search_matches_expiry_date(self) -> None:
        self._fixture()
        rows = self._data("2026-06-15")["rows"]
        self.assertEqual([row["name"] for row in rows], ["Soy sauce"])

    def test_unmatched_search_returns_empty_but_keeps_summary(self) -> None:
        self._fixture()
        data = self._data("zzzz-nothing")
        self.assertEqual(data["rows"], [])
        self.assertEqual(data["shown"], 0)
        self.assertEqual(data["total"], 8)
        self.assertEqual(data["summary"]["total_active"], 8)
        self.assertEqual(data["search"], "zzzz-nothing")

    # ------------------------------------------------------------------
    # Result shape (what pages/monitoring.py reads)
    # ------------------------------------------------------------------
    def test_result_contains_every_key_the_page_needs(self) -> None:
        expected = {
            "today", "warning_days", "filter", "search", "summary", "rows",
            "all_rows", "shown", "total", "stock_alert_rows",
            "expiry_alert_rows",
        }
        self.assertTrue(expected.issubset(self._data()))

    def test_each_row_contains_every_column_the_table_needs(self) -> None:
        row = self._data()["rows"][0]
        expected = {
            "ingredient_id", "name", "unit", "quantity_on_hand",
            "minimum_stock", "expiration_date", "status", "expiry_status",
            "days_left", "expiry_text", "alert_reasons", "needs_attention",
            "severity",
        }
        self.assertTrue(expected.issubset(row))

    def test_get_alert_counts_matches_the_summary(self) -> None:
        self._fixture()
        self.assertEqual(
            monitoring.get_alert_counts(today=TODAY, db_path=self.db_path),
            self._data()["summary"],
        )

    # ------------------------------------------------------------------
    # THE key rule: monitoring never writes anything
    # ------------------------------------------------------------------
    def test_monitoring_does_not_change_the_database_file(self) -> None:
        self._fixture()
        before = self._hash()
        for label in monitoring.FILTER_CODES:
            self._data("", label)
            self._data("pork", label)
        monitoring.get_active_ingredients(TODAY, db_path=self.db_path)
        monitoring.get_alert_counts(TODAY, db_path=self.db_path)
        self.assertEqual(self._hash(), before)

    def test_monitoring_does_not_change_balances_or_expiry_dates(self) -> None:
        self._fixture()
        sql = (
            "SELECT ingredient_id, name, quantity_on_hand, minimum_stock, "
            "expiration_date, is_active, updated_at "
            "FROM ingredients ORDER BY ingredient_id"
        )
        before = self._raw(sql)
        for value in ("", "pork", "expired", "2026-06-15"):
            self._data(value)
        self.assertEqual(self._raw(sql), before)

    def test_monitoring_creates_no_stock_transactions(self) -> None:
        self._fixture()
        sql = (
            "SELECT (SELECT COUNT(*) FROM stock_in_transactions), "
            "(SELECT COUNT(*) FROM stock_out_transactions), "
            "(SELECT COUNT(*) FROM vw_stock_movements)"
        )
        before = self._raw(sql)
        for _ in range(5):
            self._data("", monitoring.FILTER_CRITICAL)
        self.assertEqual(self._raw(sql), before)

    def test_monitoring_works_on_a_read_only_connection(self) -> None:
        """A mode=ro SQLite connection proves no write is ever attempted."""
        def read_only(_path: Any) -> sqlite3.Connection:
            return sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)

        with patch("database.get_connection", side_effect=read_only):
            data = self._data()
        self.assertEqual(data["total"], 8)
        self.assertEqual(data["summary"]["out_of_stock"], 8)

    def test_monitoring_never_opens_a_write_transaction(self) -> None:
        with patch(
            "database.transaction",
            side_effect=AssertionError("monitoring must not open a write transaction"),
        ):
            data = self._data()
        self.assertEqual(data["total"], 8)

    @staticmethod
    def _sql_write_literals(module_file: str | Path) -> list[str]:
        """Return string literals that look like SQL write statements."""
        tree = ast.parse(Path(module_file).read_text(encoding="utf-8"))
        docstrings: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                body = getattr(node, "body", [])
                if (body and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)):
                    docstrings.add(id(body[0].value))
        return [
            statement
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            for statement in [" ".join(node.value.strip().split()).lower()]
            if statement.startswith(
                ("insert ", "update ", "delete ", "replace ",
                 "create ", "drop ", "alter ")
            ) or statement in ("insert", "update", "delete", "create", "drop")
        ]

    def test_module_source_contains_no_write_sql(self) -> None:
        self.assertEqual(self._sql_write_literals(monitoring.__file__), [])

    def test_page_source_contains_no_sql(self) -> None:
        page = Path(monitoring.__file__).parent / "pages" / "monitoring.py"
        self.assertEqual(self._sql_write_literals(page), [])
        text = page.read_text(encoding="utf-8").lower()
        for keyword in ("select ", "insert ", "update ", "delete ", "sqlite3.connect"):
            self.assertNotIn(keyword, text)

    # ------------------------------------------------------------------
    # Live data integration - monitoring always reflects current SQLite
    # ------------------------------------------------------------------
    def _stock_in_all(self, quantity: float, expiry: str = "2026-12-31") -> None:
        lines = [
            {"ingredient_id": int(row["ingredient_id"]),
             "quantity": quantity, "expiration_date": expiry}
            for row in repository.list_ingredients(db_path=self.db_path)
        ]
        stock.record_stock_in(lines, user_id=1, db_path=self.db_path)

    def test_stock_in_clears_out_of_stock_alerts(self) -> None:
        self.assertEqual(self._data()["summary"]["out_of_stock"], 8)
        self._stock_in_all(5000)
        summary = self._data()["summary"]
        self.assertEqual(summary["out_of_stock"], 0)
        self.assertEqual(summary["needs_attention"], 0)
        self.assertEqual(summary["healthy"], 8)

    def test_stock_out_recreates_an_alert(self) -> None:
        self._stock_in_all(5000)
        ingredient = repository.list_ingredients("Pork", db_path=self.db_path)[0]
        stock.record_stock_out(
            [{"ingredient_id": int(ingredient["ingredient_id"]), "quantity": 5000}],
            "Consumption", user_id=1, db_path=self.db_path,
        )
        data = self._data()
        self.assertEqual(self._row("Pork", data)["status"], "Out of stock")
        self.assertEqual(
            self._row("Pork", data)["severity"], monitoring.SEVERITY_CRITICAL
        )
        self.assertEqual(data["summary"]["out_of_stock"], 1)

    def test_repository_edit_changes_monitoring(self) -> None:
        self._stock_in_all(5000)
        ingredient = repository.list_ingredients("Egg", db_path=self.db_path)[0]
        # A larger minimum with the same balance flips "In stock" -> "Low stock".
        repository.update_ingredient(
            int(ingredient["ingredient_id"]), "Egg", "piece", 10,
            TODAY_ISO, db_path=self.db_path,
        )
        self.assertEqual(self._row("Egg")["status"], "In stock")
        repository.update_ingredient(
            int(ingredient["ingredient_id"]), "Egg", "piece", 5000,
            TODAY_ISO, db_path=self.db_path,
        )
        row = self._row("Egg")
        self.assertEqual(row["status"], "Low stock")
        self.assertEqual(row["expiry_status"], "Near expiry")
        self.assertEqual(row["severity"], monitoring.SEVERITY_WARNING)
        self.assertEqual(row["expiry_text"], "2026-06-15 · today")

    def test_soft_deleted_ingredients_are_excluded(self) -> None:
        ingredient = repository.list_ingredients("Garlic", db_path=self.db_path)[0]
        repository.delete_ingredient(
            int(ingredient["ingredient_id"]), db_path=self.db_path
        )
        data = self._data()
        self.assertEqual(data["total"], 7)
        self.assertNotIn("Garlic", [row["name"] for row in data["rows"]])
        self.assertEqual(data["summary"]["total_active"], 7)
        self.assertEqual(data["summary"]["out_of_stock"], 7)

    def test_integrity_and_foreign_keys_stay_clean(self) -> None:
        self._fixture()
        for _ in range(3):
            self._data("", monitoring.FILTER_ATTENTION)
        with database.transaction(self.db_path) as conn:
            self.assertEqual(
                conn.execute("PRAGMA integrity_check").fetchone()[0], "ok"
            )
            self.assertEqual(
                conn.execute("PRAGMA foreign_key_check").fetchall(), []
            )


if __name__ == "__main__":
    unittest.main()
