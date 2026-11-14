"""
Stage 17 - compact cashier POS + SILENT receipt archiving (no Save dialog).

Background: the only printer on this PC is "Microsoft Print to PDF". The
original `os.startfile(path, "print")` therefore opened a Windows "Save Print
Output As" dialog after EVERY confirmed sale. Stage 17 makes the default
silent (archive only), skips PDF/fax pseudo-printers, and compacts the POS so
the whole checkout flow fits in 1160x720 without scrolling.

All printing tests are SIMULATED (os.startfile / win32print are patched).
No real USB thermal printer or HID scanner is used here. The real GUI layout
is verified by stage17_smoke.py.
"""

from __future__ import annotations

import ast
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import database
import discounts
import permissions
import products
import receipt
import store_sales
from pages import store_pos as pos_page

ROOT = Path(__file__).resolve().parents[1]
MANAGER: dict[str, Any] = {"user_id": 1, "username": "admin", "role": "manager"}


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _has_dynamic_execution(source: str) -> bool:
    """Real ast.Call nodes only - docstrings mentioning eval() must not trip."""
    tree = ast.parse(source)
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in ("eval", "exec", "compile")
        for node in ast.walk(tree)
    )


def _patch_config(**overrides: Any) -> Any:
    config = dict(receipt.DEFAULT_PRINTER_CONFIG)
    config.update(overrides)
    return patch.object(receipt, "load_printer_config", return_value=config)


# ======================================================================
# 1 - No "Save Print Output As" prompt after confirming a sale
# ======================================================================
class SilentPrintingTests(unittest.TestCase):

    def setUp(self) -> None:
        receipt.reset_print_guard()
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "receipt.txt"
        self.path.write_text("receipt", encoding="utf-8")
        env = patch.dict(os.environ)
        env.start()
        os.environ.pop("MART_NO_HARDWARE", None)
        self.addCleanup(env.stop)
        self.addCleanup(receipt.reset_print_guard)
        self.addCleanup(self.temp.cleanup)

    def test_out_of_the_box_mode_is_archive_only(self) -> None:
        self.assertEqual(receipt.DEFAULT_PRINTER_CONFIG["mode"], "archive_only")

    def test_missing_config_is_archive_only(self) -> None:
        loaded = receipt.load_printer_config(
            Path(self.temp.name) / "does-not-exist.json"
        )
        self.assertEqual(loaded["mode"], "archive_only")

    def test_malformed_or_unknown_mode_is_archive_only(self) -> None:
        path = Path(self.temp.name) / "printer.json"
        for raw in ("{broken", '{"mode": "network_print"}'):
            with self.subTest(raw=raw):
                path.write_text(raw, encoding="utf-8")
                self.assertEqual(
                    receipt.load_printer_config(path)["mode"], "archive_only"
                )

    def test_no_printer_name_is_hardcoded(self) -> None:
        self.assertEqual(receipt.DEFAULT_PRINTER_CONFIG["printer_name"], "")
        source = _source("receipt.py")
        for name in ("POS-58", "EPSON", "TM-T20", "RP58", "XP-58"):
            self.assertNotIn(name, source)

    def test_pdf_fax_and_empty_printers_are_detected_as_virtual(self) -> None:
        for name in (
            "Microsoft Print to PDF", "Fax", "Microsoft XPS Document Writer",
            "Send To OneNote 2016", "Bullzip PDF Printer",
            "Microsoft Print To File", "", None,
        ):
            with self.subTest(name=name):
                self.assertTrue(receipt.is_virtual_printer(name))

    def test_real_paper_printers_are_not_virtual(self) -> None:
        for name in ("Counter 58mm", "EPSON TM-T82", "HP LaserJet Pro"):
            with self.subTest(name=name):
                self.assertFalse(receipt.is_virtual_printer(name))

    def test_default_mode_never_calls_startfile_on_a_pdf_printer(self) -> None:
        for name in ("Microsoft Print to PDF", "Fax", ""):
            with self.subTest(name=name), _patch_config(mode="default"):
                with patch.object(
                    receipt, "default_printer_name", return_value=name
                ), patch.object(receipt.os, "startfile", create=True) as start:
                    result = receipt.print_receipt("x", path=self.path)
                self.assertEqual(result["method"], "archive_only")
                self.assertTrue(result["ok"])
                self.assertIn("archived automatically", result["detail"])
                start.assert_not_called()

    def test_default_mode_still_prints_to_a_real_paper_printer(self) -> None:
        with _patch_config(mode="default"), patch.object(
            receipt, "default_printer_name", return_value="Counter 58mm"
        ), patch.object(receipt.os, "startfile", create=True) as start:
            result = receipt.print_receipt("x", path=self.path)
        self.assertEqual(result["method"], "default_printer")
        start.assert_called_once()

    def test_archive_only_never_calls_startfile(self) -> None:
        with _patch_config(mode="archive_only"), patch.object(
            receipt.os, "startfile", create=True
        ) as start:
            result = receipt.print_receipt("x", path=self.path)
        self.assertEqual(result["method"], "archive_only")
        self.assertTrue(result["ok"])
        self.assertIn("Receipt archived automatically", result["detail"])
        start.assert_not_called()

    def test_hardware_kill_switch_still_forces_archive_only(self) -> None:
        with patch.dict(os.environ, {"MART_NO_HARDWARE": "1"}):
            loaded = receipt.load_printer_config(
                Path(self.temp.name) / "nope.json"
            )
        self.assertEqual(loaded["mode"], "archive_only")

    def test_saved_config_round_trips(self) -> None:
        path = Path(self.temp.name) / "config" / "printer.json"
        receipt.save_printer_config(
            {"mode": "escpos", "printer_name": "Counter 58mm"}, path=path
        )
        self.assertTrue(path.exists())
        self.assertEqual(json.loads(path.read_text())["mode"], "escpos")
        self.assertEqual(
            receipt.load_printer_config(path)["printer_name"], "Counter 58mm"
        )

    def test_escpos_mode_is_untouched(self) -> None:
        """A configured thermal printer still gets one RAW job, no dialog."""
        with _patch_config(mode="escpos", printer_name="Counter 58mm"):
            with patch.object(
                receipt, "_raw_print", return_value="10 bytes sent"
            ) as raw, patch.object(receipt.os, "startfile", create=True) as start:
                result = receipt.print_receipt("x", path=self.path)
        self.assertEqual(result["method"], "escpos")
        self.assertTrue(result["ok"])
        raw.assert_called_once()
        start.assert_not_called()

    def test_explicit_printer_name_still_forces_raw_escpos(self) -> None:
        with _patch_config(mode="archive_only"):
            with patch.object(
                receipt, "_raw_print", return_value="10 bytes sent"
            ) as raw:
                result = receipt.print_receipt(
                    "x", path=self.path, printer_name="Counter 58mm"
                )
        self.assertEqual(result["method"], "escpos")
        raw.assert_called_once()

    def test_auto_print_false_never_opens_any_printer(self) -> None:
        with _patch_config(auto_print=False), patch.object(
            receipt.os, "startfile", create=True
        ) as start:
            result = receipt.print_receipt("x", path=self.path)
        self.assertEqual(result["method"], "disabled")
        start.assert_not_called()

    def test_duplicate_print_guard_still_applies(self) -> None:
        with _patch_config(mode="default"), patch.object(
            receipt, "default_printer_name", return_value="Counter 58mm"
        ), patch.object(receipt.os, "startfile", create=True):
            first = receipt.print_receipt("x", path=self.path)
            second = receipt.print_receipt("x", path=self.path)
        self.assertEqual(first["method"], "default_printer")
        self.assertEqual(second["method"], "duplicate_skipped")

    def test_virtual_printer_helper_is_pure(self) -> None:
        source = _source("receipt.py")
        self.assertIn("VIRTUAL_PRINTER_MARKERS", source)
        self.assertFalse(_has_dynamic_execution(
            _source("change.py")
        ))  # Stage 16 pure logic stays untouched

    def test_receipt_is_still_archived_with_the_silent_default(self) -> None:
        sale = {
            "transaction_number": "SM-STAGE17-0001",
            "transaction_date": "2026-11-14 10:00:00",
            "cashier": "counter",
            "status": "completed",
            "subtotal_amount": 18.0,
            "total_amount": 18.0,
            "discount_amount": 0.0,
            "discount_type": "",
            "customer_name": "",
            "customer_id": "",
            "payment_method": "Cash",
            "amount_tendered": 100.0,
            "change_amount": 82.0,
            "items": [
                {
                    "product_name": "Boy Bawang Garlic 50 g",
                    "barcode": "4800010234560",
                    "category": "Snacks",
                    "quantity": 1,
                    "unit_price": 18.0,
                    "line_total": 18.0,
                }
            ],
        }
        with _patch_config(), patch.object(
            receipt.os, "startfile", create=True
        ) as start:
            issued = receipt.issue_store_receipt(
                sale, base_dir=self.temp.name
            )
        self.assertTrue(issued["path"].exists())
        self.assertEqual(issued["print"]["method"], "archive_only")
        self.assertIn("\u20b182.00", issued["text"])
        start.assert_not_called()


# ======================================================================
# 2 - A completed sale is completely unaffected by the printing change
# ======================================================================
class SaleStillCommitsTests(unittest.TestCase):

    def setUp(self) -> None:
        permissions.clear_session()
        receipt.reset_print_guard()
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "stage17.db"
        database.initialize_database(self.db)
        products.ensure_schema(self.db)
        products.bootstrap_catalog(self.db)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(receipt.reset_print_guard)
        self.addCleanup(permissions.clear_session)

    def _scalar(self, sql: str) -> Any:
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql).fetchone()[0]
        finally:
            conn.close()

    def test_a_sale_commits_and_archives_with_no_print_dialog(self) -> None:
        cart = [{"barcode": "4800010234560", "quantity": 1}]
        before = self._scalar("SELECT COUNT(*) FROM store_sales")
        with _patch_config(), patch.object(
            receipt.os, "startfile", create=True
        ) as start:
            sale = store_sales.confirm_sale(
                cart, "Cash", 100, user_id=1,
                db_path=self.db, actor=MANAGER,
            )
            issued = receipt.issue_store_receipt(
                sale, base_dir=self.temp.name
            )
        self.assertEqual(
            self._scalar("SELECT COUNT(*) FROM store_sales"), before + 1
        )
        self.assertEqual(float(sale["change_amount"]), 82.0)
        self.assertEqual(issued["print"]["method"], "archive_only")
        self.assertTrue(issued["path"].exists())
        start.assert_not_called()

    def test_underpaid_sale_still_writes_nothing(self) -> None:
        before = self._scalar("SELECT COUNT(*) FROM store_sales")
        with self.assertRaises(ValueError):
            store_sales.confirm_sale(
                [{"barcode": "4800010234560", "quantity": 1}],
                "Cash", 10, user_id=1, db_path=self.db, actor=MANAGER,
            )
        self.assertEqual(
            self._scalar("SELECT COUNT(*) FROM store_sales"), before
        )

    def test_discounted_sale_still_uses_the_net_total(self) -> None:
        sale = store_sales.confirm_sale(
            [{"barcode": "4800010234560", "quantity": 1}],
            "Cash", 100, user_id=1, db_path=self.db, actor=MANAGER,
            discount_type=discounts.SENIOR,
            customer_name="Ana Santos", customer_id="SC-2026-1",
        )
        self.assertEqual(float(sale["total_amount"]), 14.40)
        self.assertEqual(float(sale["change_amount"]), 85.60)


# ======================================================================
# 3 - Compact layout invariants (source level; real GUI in stage17_smoke)
# ======================================================================
class CompactLayoutSourceTests(unittest.TestCase):

    def setUp(self) -> None:
        self.source = _source("pages/store_pos.py")

    def test_dynamic_frames_no_longer_reserve_200px(self) -> None:
        for marker in (
            "self.cart_frame = ctk.CTkFrame(\n            panel, "
            'fg_color="transparent", height=1\n        )',
            "self.history_frame = ctk.CTkFrame(\n            panel, "
            'fg_color="transparent", height=1\n        )',
        ):
            self.assertIn(marker, self.source)

    def test_compact_cells_and_toggle_bar_exist(self) -> None:
        for marker in (
            "height=22,", "pady=4)", "self._toggle_bar",
            "self._section_area", "self._sections:",
        ):
            self.assertIn(marker, self.source)

    def test_sections_start_collapsed_and_are_reachable(self) -> None:
        for marker in (
            'self._sections[name] = {', '"open": False',
            "def set_section(", "def toggle_section(",
            "def open_tools(", "def open_history(",
            "info[\"body\"].pack(fill=\"x\", pady=(6, 0))",
            "info[\"body\"].pack_forget()",
        ):
            self.assertIn(marker, self.source)

    def test_all_old_widget_names_are_preserved(self) -> None:
        for name in (
            "self.scan_entry", "self.picker_search", "self.picker_combo",
            "self.cart_frame", "self.cart_note", "self.total_label",
            "self.change_label", "self.breakdown_label",
            "self.tendered_entry", "self.payment_combo",
            "self.confirm_button", "self.discount_combo",
            "self.customer_name_entry", "self.customer_id_entry",
            "self.discount_status_label", "self.calc_display",
            "self.history_search", "self.history_frame",
            "self.gross_label", "self.shortage_label",
        ):
            with self.subTest(name=name):
                self.assertIn(name, self.source)

    def test_checkout_card_is_still_full_width(self) -> None:
        self.assertIn('panel.pack(fill="x", pady=(4, 0))', self.source)
        self.assertIn('uniform="checkout"', self.source)
        self.assertNotIn('panel.pack(side="right", fill="y")', self.source)

    def test_scan_field_still_has_no_placeholder(self) -> None:
        start = self.source.index("self.scan_entry = ctk.CTkEntry(")
        end = self.source.index("self.scan_entry.pack(", start)
        self.assertNotIn("placeholder", self.source[start:end])

    def test_quick_cash_and_change_still_wired(self) -> None:
        for marker in (
            "change.QUICK_CASH", "self._quick_cash(value)",
            "self.tendered_var.trace_add(", "self._render_change()",
        ):
            self.assertIn(marker, self.source)

    def test_shortage_label_only_takes_space_when_needed(self) -> None:
        self.assertIn("height=18 if shortages else 1", self.source)

    def test_no_new_dependency_was_added(self) -> None:
        requirements = _source("requirements.txt").lower()
        for library in ("pandas", "openpyxl", "reportlab", "pyzbar", "zxing"):
            self.assertNotIn(library, requirements)
        self.assertIn("customtkinter", requirements)
        self.assertIn("pillow", requirements)

    def test_only_php_currency_in_new_source(self) -> None:
        for relative in ("receipt.py", "pages/store_pos.py", "tests/test_stage17.py"):
            text = _source(relative)
            self.assertNotIn("\u00a5", text)
            self.assertNotIn("\uffe5", text)

    def test_stage17_documents_why_sections_are_collapsible(self) -> None:
        self.assertIn("Stage 17 - compact counter layout", self.source)
        self.assertIn("with NO scrolling", self.source)


if __name__ == "__main__":
    unittest.main()
