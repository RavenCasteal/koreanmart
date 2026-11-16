"""
Tests for fresh_data_reset.py - client deployment data reset.

These tests verify:
  - The dry-run preview accurately reports what will be cleared/preserved.
  - The executed reset clears all demo data while preserving users/app_meta/schema.
  - FK-safe deletion order prevents constraint violations.
  - A verified backup is created before any data is touched.
  - The app can initialize the database after reset.
  - Double-reset is a no-op (idempotent).
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import database
import fresh_data_reset


def _copy_real_db(dest: Path) -> None:
    """Copy the real (workspace) DB to a temp location for testing."""
    src = database.DB_PATH
    shutil.copy2(src, dest)


class PreviewTests(unittest.TestCase):
    """Dry-run preview should accurately describe the database state."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "test.db"
        _copy_real_db(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_preview_does_not_modify_the_database(self):
        """Preview opens read-only; row counts must not change."""
        before = fresh_data_reset.preview(self.db_path)
        after = fresh_data_reset.preview(self.db_path)
        self.assertEqual(
            before["total_rows_to_clear"], after["total_rows_to_clear"]
        )

    def test_preview_lists_all_tables_to_be_cleared(self):
        report = fresh_data_reset.preview(self.db_path)
        table_names = [item["table"] for item in report["clear_breakdown"]]
        # Must include all the data tables (the ones that exist pre-migration)
        for expected in [
            "store_sale_items",
            "store_sales",
            "stock_out_details",
            "stock_in_details",
            "stock_out_transactions",
            "stock_in_transactions",
            "recipe_ingredients",
            "recipes",
            "menu_items",
            "ingredients",
        ]:
            self.assertIn(expected, table_names)

    def test_preview_preserves_users_table(self):
        report = fresh_data_reset.preview(self.db_path)
        preserve_names = [item["table"] for item in report["preserve_breakdown"]]
        self.assertIn("users", preserve_names)
        self.assertIn("app_meta", preserve_names)

    def test_preview_shows_actual_users(self):
        report = fresh_data_reset.preview(self.db_path)
        usernames = [u["username"] for u in report["users"]]
        self.assertIn("admin", usernames)

    def test_preview_shows_actual_products(self):
        report = fresh_data_reset.preview(self.db_path)
        # The real DB has 18 active sellable products
        self.assertGreaterEqual(len(report["products"]), 18)

    def test_preview_shows_actual_sales(self):
        report = fresh_data_reset.preview(self.db_path)
        # The real DB has 12 completed sales
        self.assertGreaterEqual(len(report["sales"]), 12)

    def test_preview_total_rows_matches_sum_of_breakdown(self):
        report = fresh_data_reset.preview(self.db_path)
        manual_sum = sum(item["rows"] for item in report["clear_breakdown"])
        self.assertEqual(manual_sum, report["total_rows_to_clear"])


class ExecuteResetTests(unittest.TestCase):
    """The actual reset must clear data, preserve users, and pass integrity."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "test.db"
        _copy_real_db(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_reset_clears_all_data_tables(self):
        result = fresh_data_reset.execute_reset(self.db_path)
        self.assertTrue(result["ok"])
        self.assertGreater(result["total_deleted"], 0)

        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            for table, _ in fresh_data_reset.CLEAR_TABLES:
                count = conn.execute(
                    f'SELECT COUNT(*) FROM "{table}"'
                ).fetchone()[0]
                self.assertEqual(
                    count,
                    0,
                    f"Table {table} still has {count} rows after reset",
                )
        finally:
            conn.close()

    def test_reset_preserves_users(self):
        fresh_data_reset.execute_reset(self.db_path)
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            users = [
                dict(r)
                for r in conn.execute(
                    "SELECT user_id, username, role FROM users ORDER BY user_id"
                ).fetchall()
            ]
            self.assertEqual(len(users), 2)
            self.assertEqual(users[0]["username"], "admin")
            self.assertEqual(users[0]["role"], "manager")
            self.assertEqual(users[1]["username"], "cashier")
        finally:
            conn.close()

    def test_reset_preserves_app_meta(self):
        fresh_data_reset.execute_reset(self.db_path)
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            # app_meta should still have the migration markers
            count = conn.execute(
                "SELECT COUNT(*) FROM app_meta"
            ).fetchone()[0]
            self.assertGreaterEqual(count, 3)
            for key in ["stage12_schema", "catalog_seeded", "stage15_schema"]:
                row = conn.execute(
                    "SELECT value FROM app_meta WHERE key = ?", (key,)
                ).fetchone()
                self.assertIsNotNone(row, f"app_meta key {key} was deleted")
        finally:
            conn.close()

    def test_reset_preserves_user_version(self):
        fresh_data_reset.execute_reset(self.db_path)
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            self.assertEqual(version, 4)
        finally:
            conn.close()

    def test_reset_passes_integrity_check(self):
        fresh_data_reset.execute_reset(self.db_path)
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
            self.assertEqual(integrity, "ok")
            broken = conn.execute("PRAGMA foreign_key_check").fetchall()
            self.assertEqual(len(broken), 0)
        finally:
            conn.close()

    def test_reset_creates_verified_backup(self):
        result = fresh_data_reset.execute_reset(self.db_path)
        backup_path = Path(result["backup_path"])
        self.assertTrue(backup_path.is_file())
        # Verify the backup is a valid SQLite DB
        conn = sqlite3.connect(f"{backup_path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
            self.assertEqual(integrity, "ok")
            # Backup should still have the data (taken before reset)
            count = conn.execute(
                "SELECT COUNT(*) FROM store_sales"
            ).fetchone()[0]
            self.assertGreater(count, 0)
        finally:
            conn.close()

    def test_reset_resets_autoincrement_sequences(self):
        """After reset, autoincrement sequences for cleared tables should be 0.

        Note: ensure_schema() runs after reset and may create new tables
        (stock_batches, suppliers, etc.) which get a sqlite_sequence entry
        with seq=0. That's correct — the key is that no real data has been
        inserted. For tables that existed BEFORE the reset, the sequence
        entry should be gone entirely.
        """
        fresh_data_reset.execute_reset(self.db_path)
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            # Tables that existed before the reset should have no sequence entry
            pre_existing = [
                "store_sale_items", "store_sales", "stock_out_details",
                "stock_in_details", "stock_out_transactions",
                "stock_in_transactions", "recipe_ingredients", "recipes",
                "menu_items", "ingredients",
            ]
            for table in pre_existing:
                row = conn.execute(
                    "SELECT seq FROM sqlite_sequence WHERE name = ?", (table,)
                ).fetchone()
                self.assertIsNone(
                    row,
                    f"sqlite_sequence still has an entry for {table}",
                )

            # Tables created by ensure_schema after reset may have seq=0
            # (that's fine — 0 means no rows inserted yet)
            for table in ["stock_batches", "stock_batch_allocations", "suppliers"]:
                row = conn.execute(
                    "SELECT seq FROM sqlite_sequence WHERE name = ?", (table,)
                ).fetchone()
                if row is not None:
                    self.assertEqual(
                        row[0], 0,
                        f"sqlite_sequence for {table} has seq={row[0]}, expected 0",
                    )
        finally:
            conn.close()

    def test_double_reset_is_idempotent(self):
        """Running the reset twice should delete 0 rows the second time."""
        first = fresh_data_reset.execute_reset(self.db_path)
        self.assertGreater(first["total_deleted"], 0)

        second = fresh_data_reset.execute_reset(self.db_path)
        self.assertEqual(second["total_deleted"], 0)


class VerifyAppStartsTests(unittest.TestCase):
    """Post-reset, the app's initialization must succeed."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "test.db"
        _copy_real_db(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_verify_app_starts_after_reset(self):
        fresh_data_reset.execute_reset(self.db_path)
        report = fresh_data_reset.verify_app_starts(self.db_path)
        self.assertTrue(report["ok"])
        self.assertEqual(report["integrity"], "ok")
        self.assertTrue(report["all_data_cleared"])

    def test_verify_app_starts_preserves_users(self):
        fresh_data_reset.execute_reset(self.db_path)
        report = fresh_data_reset.verify_app_starts(self.db_path)
        usernames = [u["username"] for u in report["users"]]
        self.assertIn("admin", usernames)
        self.assertIn("cashier", usernames)

    def test_verify_creates_stage18_tables(self):
        """After reset + ensure_schema, Stage 18 tables should exist."""
        fresh_data_reset.execute_reset(self.db_path)
        report = fresh_data_reset.verify_app_starts(self.db_path)
        # ensure_schema runs during verify_app_starts
        self.assertIn("stock_batches", report["counts"])
        self.assertIn("stock_batch_allocations", report["counts"])
        self.assertIn("suppliers", report["counts"])
        self.assertEqual(report["counts"]["stock_batches"], 0)
        self.assertEqual(report["counts"]["suppliers"], 0)


class DoesNotRunOnStartupTests(unittest.TestCase):
    """The reset must NOT run automatically on app startup."""

    def test_main_py_does_not_import_fresh_data_reset(self):
        """main.py must not import or call fresh_data_reset."""
        main_path = database.BASE_DIR / "main.py"
        source = main_path.read_text(encoding="utf-8")
        self.assertNotIn("fresh_data_reset", source)
        self.assertNotIn("reset_demo_data", source)

    def test_no_auto_reset_in_database_initialize(self):
        """database.initialize_database must not call any reset."""
        import inspect

        source = inspect.getsource(database.initialize_database)
        self.assertNotIn("fresh_data_reset", source)
        self.assertNotIn("DELETE FROM", source.lower())


if __name__ == "__main__":
    unittest.main()
