"""
Stage 13 automated checks - sales reports, daily Z-read and CSV export.

Every test uses a FRESH temporary SQLite database and a temporary reports
folder. The real database/ramen_inventory.db and the delivered reports/
folder are never opened for writing here.

Verified behaviours:
  1. strict date handling and date navigation
  2. daily aggregation from immutable sale snapshots (gross / voids / net)
  3. payment, cashier, category and top-product breakdowns
  4. voids stay in history but never inflate net sales or items sold
  5. a 7-day trend with zero-filled days
  6. printable PHP Z-read + CSV export (no new dependency)
  7. manager-only capability, page wiring and source hygiene
  8. reporting is strictly read-only
"""

from __future__ import annotations

import csv
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import database
import permissions
import products
import sales_report
import store_sales
import users_repository
from components.shell import PAGES
from formatting import CURRENCY_CODE, CURRENCY_SYMBOL, format_money

ROOT = Path(__file__).resolve().parents[1]
MANAGER: dict[str, Any] = {"user_id": 1, "username": "admin", "role": "manager"}
CASHIER: dict[str, Any] = {"user_id": 2, "username": "cashier", "role": "cashier"}

COKE = "4800001234567"        # Coca-Cola Original 500 ml, Beverages, 35.00
WATER = "4800002234564"       # Pure Drinking Water 500 ml, Beverages, 15.00
CLOUD9 = "4800005234565"      # Cloud 9 Classic Chocolate, Sweets, 12.00


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


class _Stage13Case(unittest.TestCase):
    def setUp(self) -> None:
        permissions.clear_session()
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "stage13.db"
        self.out = Path(self.temp.name) / "reports"
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
        rows = self._query(sql, params)
        return None if not rows else list(rows[0].values())[0]

    def _sale(
        self,
        cart: Any,
        method: str = "Cash",
        user_id: Any = 1,
        tendered: Any = "5000",
    ) -> dict[str, Any]:
        return store_sales.confirm_sale(
            cart, method, tendered,
            user_id=user_id, db_path=self.db_path, actor=MANAGER,
        )

    def _void(self, sale_id: Any, reason: str = "Customer changed mind") -> None:
        store_sales.void_sale(
            sale_id, reason,
            user_id=1, db_path=self.db_path, actor=MANAGER,
        )

    def _set_date(self, sale_id: Any, day: str, time: str = "12:34:56") -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "UPDATE store_sales SET transaction_date = ? WHERE sale_id = ?",
                    (f"{day} {time}", sale_id),
                )
        finally:
            conn.close()

    def _snapshot(self) -> tuple[Any, ...]:
        return (
            self._scalar("SELECT COUNT(*) FROM store_sales"),
            self._scalar("SELECT COUNT(*) FROM store_sale_items"),
            self._scalar("SELECT COUNT(*) FROM stock_out_details"),
            self._scalar("SELECT COUNT(*) FROM stock_in_details"),
            self._scalar("SELECT ROUND(SUM(quantity_on_hand), 4) FROM ingredients"),
            self._scalar("SELECT COUNT(*) FROM users"),
        )

    def _two_sales(self) -> tuple[dict[str, Any], dict[str, Any]]:
        first = self._sale([(COKE, 2), (CLOUD9, 1)])       # 82.00, 3 items
        second = self._sale([(WATER, 3)], method="Card")   # 45.00, 3 items
        return first, second


# ======================================================================
# 1 - Dates
# ======================================================================
class DateTests(_Stage13Case):

    def test_none_and_blank_mean_today_local_time(self) -> None:
        today = date.today().isoformat()
        self.assertEqual(sales_report.normalize_date(), today)
        self.assertEqual(sales_report.normalize_date("   "), today)

    def test_accepts_date_datetime_and_database_timestamp(self) -> None:
        self.assertEqual(sales_report.normalize_date("2026-11-12"), "2026-11-12")
        self.assertEqual(
            sales_report.normalize_date("2026-11-12 08:09:10"), "2026-11-12"
        )
        self.assertEqual(
            sales_report.normalize_date(date(2026, 11, 12)), "2026-11-12"
        )
        self.assertEqual(
            sales_report.normalize_date(datetime(2026, 11, 12, 23, 59)),
            "2026-11-12",
        )

    def test_rejects_loose_or_impossible_dates(self) -> None:
        for value in (
            "2026-1-2", "12/11/2026", "2026-13-01", "2026-02-30",
            "yesterday", "2026", 20261112,
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    sales_report.normalize_date(value)

    def test_shift_date_crosses_month_and_year_boundaries(self) -> None:
        self.assertEqual(sales_report.shift_date("2026-11-30", 1), "2026-12-01")
        self.assertEqual(sales_report.shift_date("2026-01-01", -1), "2025-12-31")
        self.assertEqual(sales_report.shift_date("2026-11-12", 0), "2026-11-12")
        self.assertEqual(
            sales_report.shift_date("2026-11-12", -1),
            (date(2026, 11, 12) - timedelta(days=1)).isoformat(),
        )


# ======================================================================
# 2 - Daily summary
# ======================================================================
class DailySummaryTests(_Stage13Case):

    def test_empty_day_returns_zeros_and_empty_breakdowns(self) -> None:
        summary = sales_report.daily_summary("2026-01-01", self.db_path)
        for key in (
            "gross_sales", "voided_amount", "net_sales", "average_basket",
        ):
            self.assertEqual(summary[key], 0.0, key)
        for key in (
            "transaction_count", "completed_count", "voided_count",
            "items_sold",
        ):
            self.assertEqual(summary[key], 0, key)
        for key in (
            "by_payment", "by_cashier", "by_category", "top_products",
            "transactions",
        ):
            self.assertEqual(summary[key], [], key)
        self.assertEqual(summary["date"], "2026-01-01")
        self.assertEqual(summary["currency_code"], CURRENCY_CODE)
        self.assertEqual(summary["store"], products.STORE_LABEL)
        self.assertTrue(summary["generated_at"])

    def test_completed_sales_are_aggregated_correctly(self) -> None:
        self._two_sales()
        summary = sales_report.daily_summary(db_path=self.db_path)
        self.assertEqual(summary["gross_sales"], 127.00)
        self.assertEqual(summary["voided_amount"], 0.0)
        self.assertEqual(summary["net_sales"], 127.00)
        self.assertEqual(summary["transaction_count"], 2)
        self.assertEqual(summary["completed_count"], 2)
        self.assertEqual(summary["voided_count"], 0)
        self.assertEqual(summary["items_sold"], 6)
        self.assertEqual(summary["average_basket"], 63.50)
        self.assertEqual(len(summary["transactions"]), 2)

    def test_net_sales_matches_the_pos_gross_sales_helper(self) -> None:
        self._two_sales()
        summary = sales_report.daily_summary(db_path=self.db_path)
        self.assertEqual(
            summary["net_sales"], store_sales.gross_sales(self.db_path)
        )

    def test_voids_count_in_gross_but_not_in_net_or_items_sold(self) -> None:
        _first, second = self._two_sales()
        self._void(second["sale_id"])
        summary = sales_report.daily_summary(db_path=self.db_path)
        self.assertEqual(summary["gross_sales"], 127.00)
        self.assertEqual(summary["voided_amount"], 45.00)
        self.assertEqual(summary["net_sales"], 82.00)
        self.assertEqual(summary["voided_count"], 1)
        self.assertEqual(summary["completed_count"], 1)
        self.assertEqual(summary["items_sold"], 3)
        self.assertEqual(summary["average_basket"], 82.00)

    def test_payment_mix_excludes_voided_sales(self) -> None:
        _first, second = self._two_sales()
        before = sales_report.daily_summary(db_path=self.db_path)
        self.assertEqual(
            {row["name"] for row in before["by_payment"]}, {"Cash", "Card"}
        )
        self._void(second["sale_id"])
        after = sales_report.daily_summary(db_path=self.db_path)
        self.assertEqual(after["by_payment"], [
            {"name": "Cash", "transactions": 1, "quantity": 0, "total": 82.00}
        ])

    def test_cashier_totals_group_by_account(self) -> None:
        cashier_id = users_repository.create_user(
            "counter", "Counter2026!", "Counter Staff", "cashier",
            db_path=self.db_path, actor=MANAGER,
        )
        self._sale([(COKE, 1)], user_id=1)                       # 35.00
        self._sale([(WATER, 2)], user_id=cashier_id,
                   method="E-wallet")                           # 30.00
        summary = sales_report.daily_summary(db_path=self.db_path)
        by_name = {row["name"]: row for row in summary["by_cashier"]}
        self.assertEqual(by_name["admin"]["total"], 35.00)
        self.assertEqual(by_name["counter"]["total"], 30.00)
        self.assertEqual(by_name["counter"]["transactions"], 1)

    def test_top_products_rank_by_units_and_use_snapshots(self) -> None:
        self._two_sales()
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "UPDATE ingredients SET name = 'RENAMED PRODUCT' "
                    "WHERE barcode = ?", (COKE,),
                )
        finally:
            conn.close()
        summary = sales_report.daily_summary(db_path=self.db_path)
        names = [row["name"] for row in summary["top_products"]]
        self.assertIn("Coca-Cola Original 500 ml", names)
        self.assertNotIn("RENAMED PRODUCT", names)
        self.assertEqual(summary["top_products"][0]["quantity"], 3)
        self.assertEqual(summary["top_products"][0]["total"], 45.00)

    def test_top_products_are_limited_to_five(self) -> None:
        self._sale([
            (COKE, 1), (WATER, 1), (CLOUD9, 1),
            ("4800016301016", 1), ("4800013234561", 1),
            ("4800008234566", 1),
        ])
        summary = sales_report.daily_summary(db_path=self.db_path)
        self.assertEqual(len(summary["top_products"]), 5)

    def test_category_mix_groups_by_sale_snapshot_category(self) -> None:
        self._two_sales()
        summary = sales_report.daily_summary(db_path=self.db_path)
        by_name = {row["name"]: row for row in summary["by_category"]}
        self.assertEqual(by_name["Beverages"]["quantity"], 5)
        self.assertEqual(by_name["Beverages"]["total"], 115.00)
        self.assertEqual(by_name["Sweets"]["quantity"], 1)
        self.assertEqual(by_name["Sweets"]["total"], 12.00)

    def test_date_filter_only_reads_the_requested_day(self) -> None:
        first, _second = self._two_sales()
        self._set_date(first["sale_id"], "2026-11-01")
        old = sales_report.daily_summary("2026-11-01", self.db_path)
        today = sales_report.daily_summary(db_path=self.db_path)
        self.assertEqual(old["transaction_count"], 1)
        self.assertEqual(old["net_sales"], 82.00)
        self.assertEqual(today["transaction_count"], 1)
        self.assertEqual(today["net_sales"], 45.00)

    def test_transactions_list_is_newest_first_and_keeps_void_reason(self) -> None:
        first, second = self._two_sales()
        self._void(second["sale_id"], "Wrong item")
        rows = sales_report.daily_summary(db_path=self.db_path)["transactions"]
        self.assertEqual(rows[0]["sale_id"], second["sale_id"])
        self.assertEqual(rows[0]["status"], "voided")
        self.assertEqual(rows[0]["void_reason"], "Wrong item")
        self.assertEqual(rows[1]["sale_id"], first["sale_id"])

    def test_summary_accepts_a_date_object(self) -> None:
        first, _second = self._two_sales()
        day = date.today()
        self._set_date(first["sale_id"], day.isoformat())
        summary = sales_report.daily_summary(day, self.db_path)
        self.assertEqual(summary["date"], day.isoformat())

    def test_reporting_never_writes_sales_or_stock_data(self) -> None:
        self._two_sales()
        before = self._snapshot()
        summary = sales_report.daily_summary(db_path=self.db_path)
        sales_report.sales_trend(7, db_path=self.db_path)
        sales_report.issue_z_read(
            summary, auto_print=False, base_dir=self.out
        )
        sales_report.export_transactions_csv(
            summary["date"], base_dir=self.out, db_path=self.db_path
        )
        self.assertEqual(self._snapshot(), before)


# ======================================================================
# 3 - Trend
# ======================================================================
class TrendTests(_Stage13Case):

    def test_trend_is_contiguous_and_zero_filled(self) -> None:
        self._two_sales()
        trend = sales_report.sales_trend(7, db_path=self.db_path)
        self.assertEqual(len(trend), 7)
        self.assertEqual(trend[-1]["date"], date.today().isoformat())
        self.assertEqual(
            trend[0]["date"],
            (date.today() - timedelta(days=6)).isoformat(),
        )
        self.assertEqual(trend[0]["transactions"], 0)
        self.assertEqual(trend[0]["net_sales"], 0.0)
        self.assertEqual(trend[-1]["transactions"], 2)
        self.assertEqual(trend[-1]["net_sales"], 127.00)

    def test_trend_ignores_voided_sales(self) -> None:
        _first, second = self._two_sales()
        self._void(second["sale_id"])
        trend = sales_report.sales_trend(7, db_path=self.db_path)
        self.assertEqual(trend[-1]["transactions"], 1)
        self.assertEqual(trend[-1]["net_sales"], 82.00)

    def test_trend_can_end_on_a_chosen_date(self) -> None:
        first, _second = self._two_sales()
        self._set_date(first["sale_id"], "2026-10-05")
        trend = sales_report.sales_trend(
            3, end_date="2026-10-06", db_path=self.db_path
        )
        self.assertEqual([row["date"] for row in trend], [
            "2026-10-04", "2026-10-05", "2026-10-06",
        ])
        self.assertEqual(trend[1]["net_sales"], 82.00)

    def test_trend_length_is_validated(self) -> None:
        for value in (0, -1, 32, "week"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    sales_report.sales_trend(value, db_path=self.db_path)


# ======================================================================
# 4 - Z-read and CSV
# ======================================================================
class OutputTests(_Stage13Case):

    def test_z_read_contains_every_section_and_peso_amounts(self) -> None:
        self._two_sales()
        summary = sales_report.daily_summary(db_path=self.db_path)
        text = sales_report.build_z_read(summary)
        for expected in (
            "NEIGHBORHOOD MART", "Z-READ", "NET SALES", "PAYMENT MIX",
            "CASHIER TOTALS", "TOP PRODUCTS", "VOIDED SALES",
            CURRENCY_CODE, CURRENCY_SYMBOL,
            format_money(127.00), format_money(63.50),
        ):
            self.assertIn(expected, text, expected)

    def test_z_read_lines_fit_the_report_width(self) -> None:
        self._two_sales()
        summary = sales_report.daily_summary(db_path=self.db_path)
        for line in sales_report.build_z_read(summary).splitlines():
            self.assertLessEqual(len(line), sales_report.REPORT_WIDTH, line)

    def test_z_read_uses_peso_only(self) -> None:
        self._two_sales()
        text = sales_report.build_z_read(
            sales_report.daily_summary(db_path=self.db_path)
        )
        self.assertIn(CURRENCY_SYMBOL, text)
        for forbidden in ("\u00a5", "\uffe5", "JPY", "CNY", "USD", "$"):
            self.assertNotIn(forbidden, text, forbidden)

    def test_z_read_lists_voided_sales_and_reasons(self) -> None:
        _first, second = self._two_sales()
        self._void(second["sale_id"], "Damaged barcode")
        text = sales_report.build_z_read(
            sales_report.daily_summary(db_path=self.db_path)
        )
        self.assertIn(second["transaction_number"], text)
        self.assertIn("Damaged barcode", text)

    def test_long_void_reasons_are_wrapped_not_truncated(self) -> None:
        reason = "Customer requested a full refund because the barcode was damaged"
        _first, second = self._two_sales()
        self._void(second["sale_id"], reason)
        text = sales_report.build_z_read(
            sales_report.daily_summary(db_path=self.db_path)
        )
        self.assertIn("Reason:", text)
        for word in reason.split():
            self.assertIn(word, text, word)
        for line in text.splitlines():
            self.assertLessEqual(len(line), sales_report.REPORT_WIDTH, line)

    def test_issue_z_read_archives_without_printing(self) -> None:
        self._sale([(COKE, 1)])
        summary = sales_report.daily_summary(db_path=self.db_path)
        outcome = sales_report.issue_z_read(
            summary, auto_print=False, base_dir=self.out
        )
        self.assertTrue(outcome["path"].exists())
        self.assertEqual(outcome["path"].name, f"Z-READ-{summary['date']}.txt")
        self.assertEqual(outcome["path"].read_text(encoding="utf-8"),
                         outcome["text"])
        self.assertEqual(outcome["print"]["method"], "skipped")
        self.assertTrue(outcome["print"]["ok"])

    def test_archiving_an_empty_day_still_works(self) -> None:
        summary = sales_report.daily_summary("2026-01-01", self.db_path)
        outcome = sales_report.issue_z_read(
            summary, auto_print=False, base_dir=self.out
        )
        self.assertIn("(no completed sales)", outcome["text"])
        self.assertTrue(outcome["path"].exists())

    def test_csv_has_header_one_row_per_sale_and_void_status(self) -> None:
        first, second = self._two_sales()
        self._void(second["sale_id"], "Returned")
        path = sales_report.export_transactions_csv(
            base_dir=self.out, db_path=self.db_path
        )
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(rows[0][0], "Transaction no.")
        self.assertEqual(rows[0][4], f"Total ({CURRENCY_CODE})")
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1][0], second["transaction_number"])
        self.assertEqual(rows[1][4], "45.00")
        self.assertEqual(rows[1][6], "voided")
        self.assertEqual(rows[1][7], "Returned")
        self.assertEqual(rows[2][0], first["transaction_number"])
        self.assertEqual(rows[2][6], "completed")

    def test_csv_filename_uses_the_selected_day(self) -> None:
        first, _second = self._two_sales()
        self._set_date(first["sale_id"], "2026-09-09")
        path = sales_report.export_transactions_csv(
            "2026-09-09", base_dir=self.out, db_path=self.db_path
        )
        self.assertEqual(path.name, "sales-2026-09-09.csv")
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(len(rows), 2)


# ======================================================================
# 5 - Roles and wiring
# ======================================================================
class AccessTests(_Stage13Case):

    def test_cashier_actor_cannot_read_summary_trend_or_outputs(self) -> None:
        self._sale([(COKE, 1)])
        summary = sales_report.daily_summary(db_path=self.db_path)
        for call in (
            lambda: sales_report.daily_summary(db_path=self.db_path,
                                               actor=CASHIER),
            lambda: sales_report.sales_trend(7, db_path=self.db_path,
                                             actor=CASHIER),
            lambda: sales_report.issue_z_read(
                summary, auto_print=False, base_dir=self.out, actor=CASHIER
            ),
            lambda: sales_report.export_transactions_csv(
                base_dir=self.out, db_path=self.db_path, actor=CASHIER
            ),
        ):
            with self.assertRaises(permissions.PermissionDenied) as caught:
                call()
            self.assertIn("view sales reports", str(caught.exception))

    def test_live_cashier_session_cannot_read_totals_without_an_actor(self) -> None:
        permissions.set_session(CASHIER)
        with self.assertRaises(permissions.PermissionDenied):
            sales_report.daily_summary(db_path=self.db_path)
        with self.assertRaises(permissions.PermissionDenied):
            sales_report.sales_trend(7, db_path=self.db_path)
        permissions.clear_session()

    def test_manager_and_legacy_internal_caller_are_allowed(self) -> None:
        self._sale([(COKE, 1)])
        self.assertTrue(sales_report.daily_summary(
            db_path=self.db_path, actor=MANAGER
        )["generated_at"])
        self.assertEqual(
            len(sales_report.sales_trend(3, db_path=self.db_path)), 3
        )

    def test_view_reports_capability_is_manager_only(self) -> None:
        self.assertIn(
            permissions.CAP_VIEW_REPORTS, permissions.ALL_CAPABILITIES
        )
        self.assertNotIn(
            permissions.CAP_VIEW_REPORTS, permissions.CASHIER_CAPABILITIES
        )
        self.assertTrue(
            permissions.can(MANAGER, permissions.CAP_VIEW_REPORTS)
        )
        self.assertFalse(
            permissions.can(CASHIER, permissions.CAP_VIEW_REPORTS)
        )
        self.assertEqual(
            permissions.CAPABILITY_LABELS[permissions.CAP_VIEW_REPORTS],
            "view sales reports",
        )

    def test_reports_page_is_registered_and_manager_only(self) -> None:
        self.assertIn("reports", PAGES)
        self.assertEqual(PAGES["reports"]["label"], "Sales Reports")
        self.assertEqual(PAGES["reports"]["view"].__name__, "ReportsView")
        self.assertTrue(callable(PAGES["reports"]["view"].refresh))
        self.assertIn("reports", permissions.MANAGER_PAGES)
        self.assertNotIn("reports", permissions.CASHIER_PAGES)
        self.assertEqual(permissions.CASHIER_PAGES, ("pos",))
        self.assertEqual(tuple(PAGES), permissions.MANAGER_PAGES)
        self.assertEqual(len(PAGES), 12)

    def test_shell_and_main_document_the_twelfth_page(self) -> None:
        self.assertIn(
            "from pages.reports import ReportsView",
            _source("components/shell.py"),
        )
        self.assertIn("12 pages", _source("main.py"))

    def test_reports_page_guards_reads_and_has_a_denied_state(self) -> None:
        source = _source("pages/reports.py")
        for expected in (
            "permissions.CAP_VIEW_REPORTS", "actor=self.actor",
            "_build_denied", "sales_report.issue_z_read(",
            "sales_report.export_transactions_csv(", "format_money(",
        ):
            self.assertIn(expected, source, expected)

    def test_reports_tables_fit_the_default_window(self) -> None:
        from pages import reports as reports_page
        groups = (
            (reports_page.SMALL_WEIGHTS, reports_page.SMALL_MINSIZES),
            (reports_page.TREND_WEIGHTS, reports_page.TREND_MINSIZES),
            (reports_page.LIST_WEIGHTS, reports_page.LIST_MINSIZES),
        )
        for weights, minsizes in groups:
            with self.subTest(weights=weights):
                inflated = max(m / w for m, w in zip(minsizes, weights))
                self.assertLessEqual(inflated * sum(weights), 820)

    def test_stage13_adds_no_third_party_dependency(self) -> None:
        requirements = _source("requirements.txt").lower()
        self.assertIn("customtkinter", requirements)
        self.assertIn("pillow", requirements)
        # pywin32 is the optional Windows printer extra from Stage 14;
        # reporting itself still uses only the standard library.
        for library in ("pandas", "openpyxl", "reportlab", "matplotlib"):
            self.assertNotIn(library, requirements)

    def test_new_modules_use_only_the_peso_sign(self) -> None:
        for relative in ("sales_report.py", "pages/reports.py"):
            with self.subTest(relative=relative):
                text = _source(relative)
                self.assertNotIn("\u00a5", text)
                self.assertNotIn("\uffe5", text)
                self.assertIn("PHP", text)

    def test_stage13_keeps_the_stage12_bootstrap_untouched(self) -> None:
        source = _source("main.py")
        self.assertIn("products.ensure_schema()", source)
        self.assertIn("products.bootstrap_catalog()", source)
        self.assertIn("12 pages", source)


if __name__ == "__main__":
    unittest.main()
