"""
Stage 14 automated checks - USB thermal printer + HID barcode scanner.

These are SIMULATED hardware tests. No physical printer or scanner is used:
  * win32print is replaced by a fake module injected into sys.modules, so the
    RAW ESC/POS job, printer discovery and offline/unplugged paths are real
    code paths exercised against a controlled fake driver.
  * Keyboard-wedge scans are simulated as keystroke bursts fed through the
    exact same scanner.ScanBuffer / PosView._on_root_key code a USB scanner
    drives. (The GUI smoke script stage14_smoke.py additionally drives real
    Tk key events.) Real hardware must still be verified on the counter PC.

Nothing here opens the delivered database/ramen_inventory.db for writing:
every transactional test uses a fresh temporary SQLite file.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import patch

import customtkinter  # noqa: F401  (imported by pages.store_pos on every run)

import database
import permissions
import products
import receipt
import scanner
import store_sales
from formatting import CURRENCY_CODE, format_money
from pages import store_pos as pos_page

ROOT = Path(__file__).resolve().parents[1]
MANAGER: dict[str, Any] = {"user_id": 1, "username": "admin", "role": "manager"}

COKE = "4800001234567"
WATER = "4800002234564"
UNKNOWN = "000000"

STORE_SALE: dict[str, Any] = {
    "transaction_number": "SM-20261113-0001",
    "transaction_date": "2026-11-13 10:00:00",
    "cashier": "cashier",
    "items": [
        {
            "product_name": "Coca-Cola Original 500 ml",
            "barcode": COKE,
            "category": "Beverages",
            "quantity": 2,
            "unit_price": 35.0,
            "line_total": 70.0,
        },
        {
            "product_name": "Cloud 9 Classic Chocolate",
            "barcode": "4800005234565",
            "category": "Sweets",
            "quantity": 1,
            "unit_price": 12.0,
            "line_total": 12.0,
        },
    ],
    "total_amount": 82.0,
    "amount_tendered": 100.0,
    "change_amount": 18.0,
    "payment_method": "Cash",
    "currency_code": CURRENCY_CODE,
    "status": "completed",
}


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


class FakeWin32Print:
    """Stand-in for win32print. Records every RAW job instead of printing."""

    PRINTER_ENUM_LOCAL = 2
    PRINTER_ENUM_CONNECTIONS = 4

    def __init__(
        self,
        names: tuple[str, ...] = ("POS-58 Thermal", "Microsoft Print to PDF"),
        status: int = 0,
        attributes: int = 0,
        default: str = "POS-58 Thermal",
        open_error: Exception | None = None,
    ) -> None:
        self.names = names
        self.status = status
        self.attributes = attributes
        self.default = default
        self.open_error = open_error
        self.written: list[bytes] = []
        self.opened: list[str] = []
        self.docs: list[tuple[Any, ...]] = []

    def EnumPrinters(self, flags: int) -> list[tuple[Any, ...]]:
        return [(None, None, name, None) for name in self.names]

    def GetDefaultPrinter(self) -> str:
        return self.default

    def OpenPrinter(self, name: str) -> object:
        if self.open_error is not None:
            raise self.open_error
        self.opened.append(name)
        return object()

    def ClosePrinter(self, handle: Any) -> None:
        return None

    def GetPrinter(self, handle: Any, level: int) -> dict[str, int]:
        return {"Status": self.status, "Attributes": self.attributes}

    def StartDocPrinter(self, handle: Any, level: int, info: Any) -> int:
        self.docs.append(info)
        return 7

    def StartPagePrinter(self, handle: Any) -> None:
        return None

    def WritePrinter(self, handle: Any, payload: bytes) -> int:
        self.written.append(payload)
        return len(payload)

    def EndPagePrinter(self, handle: Any) -> None:
        return None

    def EndDocPrinter(self, handle: Any) -> None:
        return None

    def AbortPrinter(self, handle: Any) -> None:
        return None


@contextmanager
def fake_win32(fake: FakeWin32Print) -> Iterator[FakeWin32Print]:
    with patch.dict(sys.modules, {"win32print": fake}):  # type: ignore[arg-type]
        yield fake


def config(**overrides: Any) -> dict[str, Any]:
    result = dict(receipt.DEFAULT_PRINTER_CONFIG)
    result.update(overrides)
    return result


# ======================================================================
# 1 - ESC/POS byte stream (58 mm / 32-column)
# ======================================================================
class EscposLayoutTests(unittest.TestCase):
    """SIMULATED: inspect the bytes that would be sent to a USB printer."""

    def setUp(self) -> None:
        receipt.reset_print_guard()
        self.text = receipt.build_store_receipt(STORE_SALE)

    def test_job_starts_with_initialize_and_code_page(self) -> None:
        payload = receipt.build_escpos_bytes(self.text)
        self.assertTrue(payload.startswith(b"\x1b@\x1bt\x00"))
        self.assertIn(b"\x1d!\x00", payload[:16])

    def test_alignment_commands_are_emitted(self) -> None:
        payload = receipt.build_escpos_bytes(self.text)
        self.assertIn(b"\x1ba\x01", payload)   # centred
        self.assertIn(b"\x1ba\x00", payload)   # left

    def test_bold_highlights_title_and_total(self) -> None:
        payload = receipt.build_escpos_bytes(self.text)
        self.assertIn(b"\x1bE\x01", payload)
        self.assertIn(b"\x1bE\x00", payload)

    def test_crlf_line_endings_for_thermal_printers(self) -> None:
        payload = receipt.build_escpos_bytes(self.text)
        self.assertIn(b"\r\n", payload)
        # Every LF must be preceded by CR (a bare LF would misalign a thermal
        # printer). Commands legitimately follow a CRLF.
        for index, byte in enumerate(payload):
            if byte == 0x0A:
                self.assertGreater(index, 0)
                self.assertEqual(payload[index - 1], 0x0D)

    def test_feed_and_partial_cut_at_the_end(self) -> None:
        payload = receipt.build_escpos_bytes(self.text, feed_lines=3)
        self.assertTrue(payload.endswith(b"\x1bd\x03\x1dVB\x00"))

    def test_cut_can_be_disabled(self) -> None:
        payload = receipt.build_escpos_bytes(self.text, cut=False)
        self.assertFalse(payload.endswith(b"\x1dVB\x00"))
        self.assertTrue(payload.endswith(b"\x1bd\x03"))

    def test_feed_lines_are_clamped_to_a_safe_range(self) -> None:
        low = receipt.build_escpos_bytes(self.text, feed_lines=-5)
        high = receipt.build_escpos_bytes(self.text, feed_lines=999)
        self.assertIn(b"\x1bd\x00", low)
        self.assertIn(b"\x1bd" + bytes([receipt.MAX_FEED_LINES]), high)

    def test_payload_is_single_width_ascii(self) -> None:
        payload = receipt.build_escpos_bytes(self.text)
        self.assertTrue(all(byte < 128 for byte in payload))
        self.assertIn(b"P82.00", payload)       # peso sign -> P
        self.assertNotIn("\u20b1".encode(), payload)

    def test_archived_receipt_keeps_the_real_peso_sign(self) -> None:
        self.assertIn(format_money(82.0), self.text)
        self.assertIn("\u20b1", self.text)

    def test_punctuation_is_transliterated_for_pc437(self) -> None:
        text = "\u2014 em \u2013 en \u2212 minus \u00b7 dot \u2019 quote"
        ascii_text = receipt._to_ascii(text)
        self.assertNotIn("\u2014", ascii_text)
        self.assertNotIn("\u00b7", ascii_text)
        self.assertIn("-", ascii_text)
        self.assertIn("*", ascii_text)
        self.assertTrue(all(ord(char) < 128 for char in ascii_text))

    def test_legacy_byte_alias_matches_the_new_builder(self) -> None:
        self.assertEqual(
            receipt._to_printer_bytes(self.text),
            receipt.build_escpos_bytes(self.text),
        )

    def test_every_receipt_line_fits_32_columns(self) -> None:
        for line in self.text.splitlines():
            with self.subTest(line=line):
                self.assertLessEqual(len(line), receipt.RECEIPT_WIDTH)

    def test_long_product_names_wrap_instead_of_overflowing(self) -> None:
        long_sale = dict(STORE_SALE)
        long_sale["items"] = [
            {
                "product_name": "An extremely long convenience store product "
                                "name that will never fit on fifty eight mm paper",
                "barcode": COKE,
                "quantity": 1,
                "unit_price": 35.0,
                "line_total": 35.0,
            }
        ]
        text = receipt.build_store_receipt(long_sale)
        self.assertTrue(
            all(len(line) <= receipt.RECEIPT_WIDTH for line in text.splitlines())
        )
        self.assertIn("extremely", text)

    def test_store_tagline_is_split_for_narrow_paper(self) -> None:
        self.assertEqual(len(receipt.MART_TAGLINE_LINES), 2)
        for line in receipt.MART_TAGLINE_LINES:
            self.assertLessEqual(len(line), receipt.RECEIPT_WIDTH)
        text = receipt.build_store_receipt(STORE_SALE)
        for line in receipt.MART_TAGLINE_LINES:
            self.assertIn(line.strip(), text)


class StoreReceiptFieldTests(unittest.TestCase):
    """The printed/archived receipt must carry every auditing field."""

    def test_all_required_transaction_fields_are_present(self) -> None:
        text = receipt.build_store_receipt(STORE_SALE)
        for expected in (
            STORE_SALE["transaction_number"],
            STORE_SALE["transaction_date"],
            COKE, "Coca-Cola", "2 x", "Subtotal", "TOTAL",
            "Cash", "Tendered", "Change", format_money(82.0),
            format_money(100.0), format_money(18.0), CURRENCY_CODE,
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, text)

    def test_each_item_shows_quantity_unit_price_and_line_total(self) -> None:
        text = receipt.build_store_receipt(STORE_SALE)
        self.assertIn(format_money(35.0), text)
        self.assertIn(format_money(70.0), text)
        self.assertIn(format_money(12.0), text)

    def test_no_foreign_currency_markers(self) -> None:
        text = receipt.build_store_receipt(STORE_SALE)
        for forbidden in ("\u00a5", "\uffe5", "JPY", "CNY", "USD"):
            self.assertNotIn(forbidden, text)


# ======================================================================
# 2 - Printer configuration (no hardcoded device name)
# ======================================================================
class PrinterConfigTests(unittest.TestCase):

    def setUp(self) -> None:
        # Config parsing must not depend on how the suite was launched. The
        # MART_NO_HARDWARE=1 kill switch is used for printer-less CI runs and
        # forces archive_only; these tests assert the real defaults.
        env = patch.dict(os.environ)
        env.start()
        os.environ.pop("MART_NO_HARDWARE", None)
        self.addCleanup(env.stop)

    def test_defaults_never_prompt_the_cashier_to_save_a_print_job(self) -> None:
        # Stage 17: with no receipt printer installed, Windows' default is
        # "Microsoft Print to PDF" and os.startfile(path, "print") opens a
        # "Save Print Output As" dialog after EVERY sale. Archive silently.
        defaults = receipt.DEFAULT_PRINTER_CONFIG
        self.assertEqual(defaults["mode"], "archive_only")
        self.assertEqual(defaults["printer_name"], "")
        self.assertTrue(defaults["auto_print"])
        self.assertTrue(defaults["cut"])

    def test_no_printer_name_is_hardcoded(self) -> None:
        self.assertEqual(receipt.DEFAULT_PRINTER_CONFIG["printer_name"], "")
        source = _source("receipt.py")
        for name in ("POS-58", "EPSON", "TM-T20", "RP58", "XP-58"):
            self.assertNotIn(name, source)

    def test_missing_config_file_falls_back_to_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            loaded = receipt.load_printer_config(Path(folder) / "nope.json")
        self.assertEqual(loaded["mode"], receipt.DEFAULT_PRINTER_CONFIG["mode"])
        self.assertEqual(loaded["printer_name"], "")

    def test_malformed_json_falls_back_to_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "printer.json"
            path.write_text("{not json", encoding="utf-8")
            loaded = receipt.load_printer_config(path)
        self.assertEqual(loaded["mode"], receipt.DEFAULT_PRINTER_CONFIG["mode"])

    def test_unknown_mode_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "printer.json"
            path.write_text(json.dumps({"mode": "network_print"}), encoding="utf-8")
            loaded = receipt.load_printer_config(path)
        self.assertEqual(loaded["mode"], receipt.DEFAULT_PRINTER_CONFIG["mode"])

    def test_unknown_keys_in_a_config_file_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "printer.json"
            path.write_text(
                json.dumps({"mode": "escpos", "injected": "bad"}),
                encoding="utf-8",
            )
            loaded = receipt.load_printer_config(path)
        self.assertNotIn("injected", loaded)

    def test_save_and_load_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config" / "printer.json"
            receipt.save_printer_config(
                {"mode": "escpos", "printer_name": "Front Counter",
                 "auto_print": False, "cut": False},
                path=path,
            )
            loaded = receipt.load_printer_config(path)
        self.assertEqual(loaded["mode"], "escpos")
        self.assertEqual(loaded["printer_name"], "Front Counter")
        self.assertFalse(loaded["auto_print"])
        self.assertFalse(loaded["cut"])

    def test_feed_lines_are_clamped_on_load(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "printer.json"
            path.write_text(json.dumps({"feed_lines": 500}), encoding="utf-8")
            loaded = receipt.load_printer_config(path)
        self.assertEqual(loaded["feed_lines"], receipt.MAX_FEED_LINES)

    def test_hardware_kill_switch_forces_archive_only(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "printer.json"
            path.write_text(json.dumps({"mode": "escpos"}), encoding="utf-8")
            with patch.dict(os.environ, {"MART_NO_HARDWARE": "1"}):
                loaded = receipt.load_printer_config(path)
        self.assertEqual(loaded["mode"], "archive_only")


# ======================================================================
# 3 - Printer discovery and availability (SIMULATED driver)
# ======================================================================
class PrinterDiscoveryTests(unittest.TestCase):

    def test_printer_names_come_from_enum_printers(self) -> None:
        fake = FakeWin32Print(names=("Counter 58mm", "PDF"))
        with fake_win32(fake):
            names = receipt.list_printers()
        self.assertEqual(names, ["Counter 58mm", "PDF"])

    def test_printer_list_is_sorted_case_insensitively(self) -> None:
        fake = FakeWin32Print(names=("zeta", "Alpha"))
        with fake_win32(fake):
            self.assertEqual(receipt.list_printers(), ["Alpha", "zeta"])

    def test_missing_pywin32_returns_an_empty_list(self) -> None:
        with patch.dict(sys.modules, {"win32print": None}):
            self.assertFalse(receipt.pywin32_available())
            self.assertEqual(receipt.list_printers(), [])

    def test_non_windows_platform_disables_direct_printing(self) -> None:
        with patch.object(receipt.sys, "platform", "linux"):
            self.assertEqual(receipt.list_printers(), [])
            self.assertEqual(receipt.default_printer_name(), "")
            status = receipt.check_printer("Counter 58mm")
        self.assertFalse(status["available"])
        self.assertIn("Windows", status["detail"])

    def test_empty_printer_name_reports_a_friendly_message(self) -> None:
        status = receipt.check_printer("")
        self.assertFalse(status["available"])
        self.assertIn("No receipt printer selected", status["detail"])

    def test_disconnected_printer_is_not_available(self) -> None:
        fake = FakeWin32Print(names=("Counter 58mm",))
        with fake_win32(fake):
            status = receipt.check_printer("Unplugged printer")
        self.assertFalse(status["available"])
        self.assertIn("not installed or has been disconnected", status["detail"])

    def test_offline_status_bit_is_detected(self) -> None:
        fake = FakeWin32Print(
            names=("Counter 58mm",), status=receipt.PRINTER_STATUS_OFFLINE
        )
        with fake_win32(fake):
            status = receipt.check_printer("Counter 58mm")
        self.assertFalse(status["available"])
        self.assertTrue(status["offline"])
        self.assertIn("offline", status["detail"].lower())

    def test_work_offline_attribute_is_detected(self) -> None:
        fake = FakeWin32Print(
            names=("Counter 58mm",),
            attributes=receipt.PRINTER_ATTRIBUTE_WORK_OFFLINE,
        )
        with fake_win32(fake):
            status = receipt.check_printer("Counter 58mm")
        self.assertTrue(status["offline"])
        self.assertFalse(status["available"])

    def test_ready_printer_is_available(self) -> None:
        fake = FakeWin32Print(names=("Counter 58mm",))
        with fake_win32(fake):
            status = receipt.check_printer("Counter 58mm")
        self.assertTrue(status["available"])
        self.assertFalse(status["offline"])
        self.assertIn("ready", status["detail"].lower())

    def test_open_failure_is_reported_without_raising(self) -> None:
        fake = FakeWin32Print(
            names=("Counter 58mm",), open_error=OSError("USB unplugged")
        )
        with fake_win32(fake):
            status = receipt.check_printer("Counter 58mm")
        self.assertFalse(status["available"])
        self.assertIn("Cannot open printer", status["detail"])

    def test_printer_settings_returns_everything_the_dialog_needs(self) -> None:
        fake = FakeWin32Print()
        with fake_win32(fake):
            settings = receipt.printer_settings()
        for key in ("config", "printers", "default", "pywin32", "windows"):
            self.assertIn(key, settings)
        self.assertEqual(settings["default"], "POS-58 Thermal")
        self.assertTrue(settings["pywin32"])


# ======================================================================
# 4 - Printing, failure handling and duplicate suppression (SIMULATED)
# ======================================================================
class PrintingTests(unittest.TestCase):

    def setUp(self) -> None:
        receipt.reset_print_guard()
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.path = self.folder / "receipt.txt"
        self.path.write_text("sample", encoding="utf-8")

    def tearDown(self) -> None:
        receipt.reset_print_guard()
        self.temp.cleanup()

    def _patch_config(self, **overrides: Any) -> Any:
        return patch.object(
            receipt, "load_printer_config", return_value=config(**overrides)
        )

    def test_disabled_auto_print_archives_without_touching_hardware(self) -> None:
        with self._patch_config(auto_print=False):
            with patch.object(receipt.os, "startfile") as startfile:
                result = receipt.print_receipt("x", path=self.path)
        self.assertEqual(result["method"], "disabled")
        self.assertTrue(result["ok"])
        startfile.assert_not_called()

    def test_archive_only_mode_never_opens_a_printer(self) -> None:
        with self._patch_config(mode="archive_only"):
            with patch.object(receipt.os, "startfile") as startfile:
                result = receipt.print_receipt("x", path=self.path)
        self.assertEqual(result["method"], "archive_only")
        startfile.assert_not_called()

    def test_escpos_mode_sends_one_raw_job(self) -> None:
        fake = FakeWin32Print(names=("Counter 58mm",))
        with fake_win32(fake), self._patch_config(
            mode="escpos", printer_name="Counter 58mm"
        ):
            result = receipt.print_receipt("TOTAL  P82.00", path=self.path)
        self.assertEqual(result["method"], "escpos")
        self.assertTrue(result["ok"])
        self.assertEqual(len(fake.written), 1)
        self.assertTrue(fake.written[0].startswith(b"\x1b@"))
        self.assertIn("bytes sent", result["detail"])

    def test_raw_job_uses_the_raw_datatype_and_one_document(self) -> None:
        fake = FakeWin32Print(names=("Counter 58mm",))
        with fake_win32(fake), self._patch_config(
            mode="escpos", printer_name="Counter 58mm"
        ):
            receipt.print_receipt("x", path=self.path)
        self.assertEqual(len(fake.docs), 1)
        self.assertEqual(fake.docs[0][2], "RAW")

    def test_unavailable_escpos_printer_fails_gracefully(self) -> None:
        fake = FakeWin32Print(names=("Counter 58mm",))
        with fake_win32(fake), self._patch_config(
            mode="escpos", printer_name="Counter 58mm"
        ):
            with patch.object(
                receipt, "check_printer",
                return_value={"name": "Counter 58mm", "available": False,
                              "offline": True, "detail": "Printer is offline."},
            ):
                with patch.object(receipt.os, "startfile") as startfile:
                    result = receipt.print_receipt("x", path=self.path)
        self.assertFalse(result["ok"])
        self.assertEqual(result["method"], "escpos_failed")
        self.assertIn("archived", result["detail"])
        # A failed ESC/POS job must NOT fall through to a second printer.
        startfile.assert_not_called()

    def test_no_printer_selected_fails_with_setup_hint(self) -> None:
        with self._patch_config(mode="escpos", printer_name=""):
            result = receipt.print_receipt("x", path=self.path)
        self.assertFalse(result["ok"])
        self.assertIn("Printer setup", result["detail"])

    def test_printing_never_raises_when_the_driver_throws(self) -> None:
        with self._patch_config(mode="escpos", printer_name="Counter 58mm"):
            with patch.object(
                receipt, "_raw_print", side_effect=OSError("device lost")
            ):
                result = receipt.print_receipt("x", path=self.path)
        self.assertFalse(result["ok"])
        self.assertIn("Receipt was archived", result["detail"])

    def test_duplicate_print_inside_the_guard_window_is_suppressed(self) -> None:
        fake = FakeWin32Print(names=("Counter 58mm",))
        with fake_win32(fake), self._patch_config(
            mode="escpos", printer_name="Counter 58mm"
        ):
            first = receipt.print_receipt("x", path=self.path)
            second = receipt.print_receipt("x", path=self.path)
        self.assertEqual(first["method"], "escpos")
        self.assertEqual(second["method"], "duplicate_skipped")
        self.assertEqual(len(fake.written), 1)

    def test_reprint_can_explicitly_reset_the_guard(self) -> None:
        fake = FakeWin32Print(names=("Counter 58mm",))
        with fake_win32(fake), self._patch_config(
            mode="escpos", printer_name="Counter 58mm"
        ):
            receipt.print_receipt("x", path=self.path)
            receipt.reset_print_guard()
            again = receipt.print_receipt("x", path=self.path)
        self.assertEqual(again["method"], "escpos")
        self.assertEqual(len(fake.written), 2)

    def test_default_printer_path_is_still_supported(self) -> None:
        # Stage 17: a REAL paper printer is required. A PDF/fax pseudo-printer
        # is skipped so Windows never asks the cashier where to save a file.
        with self._patch_config(mode="default"), patch.object(
            receipt, "default_printer_name", return_value="Counter 58mm"
        ):
            with patch.object(receipt.os, "startfile", create=True) as startfile:
                result = receipt.print_receipt("x", path=self.path)
        self.assertEqual(result["method"], "default_printer")
        startfile.assert_called_once()

    def test_pdf_fax_pseudo_printers_never_open_a_save_dialog(self) -> None:
        for name in ("Microsoft Print to PDF", "Fax", "Microsoft XPS Document Writer", ""):
            with self.subTest(name=name), self._patch_config(mode="default"):
                with patch.object(receipt, "default_printer_name", return_value=name):
                    with patch.object(receipt.os, "startfile", create=True) as startfile:
                        result = receipt.print_receipt("x", path=self.path)
                self.assertEqual(result["method"], "archive_only")
                self.assertTrue(result["ok"])
                self.assertIn("archived automatically", result["detail"])
                startfile.assert_not_called()

    def test_explicit_printer_name_forces_escpos(self) -> None:
        fake = FakeWin32Print(names=("Counter 58mm",))
        with fake_win32(fake), self._patch_config(mode="default"):
            result = receipt.print_receipt(
                "x", path=self.path, printer_name="Counter 58mm"
            )
        self.assertEqual(result["method"], "escpos")
        self.assertEqual(len(fake.written), 1)

    def test_test_page_does_not_create_a_sale_or_an_archive(self) -> None:
        fake = FakeWin32Print(names=("Counter 58mm",))
        with fake_win32(fake), self._patch_config(
            mode="escpos", printer_name="Counter 58mm"
        ):
            outcome = receipt.print_test_page("Counter 58mm")
        self.assertTrue(outcome["ok"])
        self.assertEqual(len(fake.written), 1)
        self.assertIn(b"PRINTER TEST", fake.written[0])
        # Only the fixture receipt.txt exists - the test page is not archived.
        self.assertEqual(len(list(self.folder.glob("*.txt"))), 1)

    def test_test_page_reports_an_unavailable_printer(self) -> None:
        fake = FakeWin32Print(names=("Counter 58mm",))
        with fake_win32(fake):
            outcome = receipt.print_test_page("Missing printer")
        self.assertFalse(outcome["ok"])
        self.assertIn("not installed", outcome["detail"])

    def test_issue_store_receipt_archives_before_printing(self) -> None:
        fake = FakeWin32Print(names=("Counter 58mm",))
        seen: list[Path] = []

        def _recording_print(text: str, path: Any = None,
                             printer_name: str | None = None) -> dict[str, Any]:
            self.assertTrue(Path(path).exists(), "archive must exist first")
            seen.append(Path(path))
            return {"method": "escpos", "detail": "OK", "ok": True}

        with fake_win32(fake), patch.object(
            receipt, "print_receipt", side_effect=_recording_print
        ):
            issued = receipt.issue_store_receipt(
                STORE_SALE, base_dir=self.folder
            )
        self.assertEqual(len(seen), 1)
        self.assertTrue(issued["path"].exists())
        self.assertIn("TOTAL", issued["text"])

    def test_a_failed_print_still_leaves_the_archived_receipt(self) -> None:
        with patch.object(
            receipt, "print_receipt",
            return_value={"method": "escpos_failed", "ok": False,
                          "detail": "Printer offline. Receipt was archived."},
        ):
            issued = receipt.issue_store_receipt(
                STORE_SALE, base_dir=self.folder
            )
        self.assertTrue(issued["path"].exists())
        self.assertFalse(issued["print"]["ok"])
        self.assertIn("\u20b1", issued["path"].read_text(encoding="utf-8"))

    def test_auto_print_false_still_archives(self) -> None:
        issued = receipt.issue_store_receipt(
            STORE_SALE, base_dir=self.folder, auto_print=False
        )
        self.assertTrue(issued["path"].exists())
        self.assertEqual(issued["print"]["method"], "skipped")


# ======================================================================
# 5 - Keyboard-wedge scanner rules (SIMULATED keystrokes)
# ======================================================================
class ScannerLogicTests(unittest.TestCase):

    def test_valid_ean13_is_accepted(self) -> None:
        self.assertEqual(scanner.normalize_barcode(COKE), COKE)

    def test_cr_lf_and_framing_characters_are_stripped(self) -> None:
        self.assertEqual(
            scanner.normalize_barcode("\x02" + COKE + "\r\n"), COKE
        )

    def test_empty_input_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Scan or type"):
            scanner.normalize_barcode("   ")

    def test_partial_scan_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Incomplete barcode"):
            scanner.normalize_barcode("4800")

    def test_overlong_input_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "too long"):
            scanner.normalize_barcode("1" * (scanner.MAX_BARCODE_LENGTH + 1))

    def test_non_barcode_characters_are_rejected(self) -> None:
        for value in ("4800 001", "4800,001", "4800@001", "4800/001"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "not a barcode"):
                    scanner.normalize_barcode(value)

    def test_alphanumeric_and_punctuation_codes_are_allowed(self) -> None:
        self.assertEqual(scanner.normalize_barcode("AB-12_34.X"), "AB-12_34.X")

    def test_minimum_length_matches_the_catalog_validator(self) -> None:
        six = "1" * scanner.MIN_BARCODE_LENGTH
        self.assertEqual(products.validate_barcode(six), six)
        with self.assertRaises(ValueError):
            products.validate_barcode("1" * (scanner.MIN_BARCODE_LENGTH - 1))

    def test_is_barcode_char_accepts_only_single_digits(self) -> None:
        self.assertTrue(scanner.is_barcode_char("7"))
        for value in ("A", "-", "12", "", None, 7):
            with self.subTest(value=value):
                self.assertFalse(scanner.is_barcode_char(value))

    def test_fast_burst_is_recognised_as_a_scanner(self) -> None:
        buf = scanner.ScanBuffer()
        for index, char in enumerate(COKE):
            buf.feed(char, index * 10.0)
        self.assertTrue(buf.active)
        self.assertEqual(buf.finish(), COKE)
        self.assertFalse(buf.active)  # finish() resets

    def test_slow_human_typing_is_not_a_scan(self) -> None:
        buf = scanner.ScanBuffer()
        for index, char in enumerate(COKE):
            buf.feed(char, index * 200.0)
        self.assertFalse(buf.active)
        self.assertIsNone(buf.finish())

    def test_a_non_digit_resets_the_burst(self) -> None:
        buf = scanner.ScanBuffer()
        for index, char in enumerate("12345"):
            buf.feed(char, index * 10.0)
        self.assertTrue(buf.active)
        self.assertFalse(buf.feed("x", 60.0))
        self.assertIsNone(buf.finish())

    def test_a_focus_change_between_bursts_is_handled_by_reset(self) -> None:
        buf = scanner.ScanBuffer()
        for index, char in enumerate("12345"):
            buf.feed(char, index * 10.0)
        buf.reset()
        self.assertFalse(buf.active)
        self.assertIsNone(buf.finish())

    def test_oversized_burst_is_discarded(self) -> None:
        buf = scanner.ScanBuffer()
        for index in range(scanner.MAX_BARCODE_LENGTH + 9):
            buf.feed("1", index * 10.0)
        self.assertFalse(buf.active)

    def test_short_burst_never_validates_as_a_barcode(self) -> None:
        buf = scanner.ScanBuffer()
        for index, char in enumerate("123"):
            buf.feed(char, index * 10.0)
        self.assertIsNone(buf.finish())  # 3 chars < MIN_BARCODE_LENGTH

    def test_debouncer_rejects_a_bounce_within_the_window(self) -> None:
        deb = scanner.ScanDebouncer()
        self.assertTrue(deb.accept(COKE, 1000.0))
        self.assertFalse(deb.accept(COKE, 1100.0))

    def test_debouncer_allows_a_deliberate_repeat_scan(self) -> None:
        deb = scanner.ScanDebouncer()
        self.assertTrue(deb.accept(COKE, 1000.0))
        self.assertTrue(deb.accept(COKE, 1200.0))

    def test_debouncer_allows_a_different_product_immediately(self) -> None:
        deb = scanner.ScanDebouncer()
        self.assertTrue(deb.accept(COKE, 1000.0))
        self.assertTrue(deb.accept(WATER, 1010.0))

    def test_debouncer_reset_forgets_the_last_code(self) -> None:
        deb = scanner.ScanDebouncer()
        deb.accept(COKE, 1000.0)
        deb.reset()
        self.assertTrue(deb.accept(COKE, 1010.0))


class _FakeEntry:
    """Minimal CTkEntry stand-in (no display needed)."""

    def __init__(self, value: str = "") -> None:
        self.value = value
        self._entry = self
        self.focus_calls = 0

    def get(self) -> str:
        return self.value

    def delete(self, first: Any, last: Any = None) -> None:
        self.value = self.value[: int(first)]

    def insert(self, index: Any, text: str) -> None:
        self.value = self.value[: int(index)] + str(text)

    def focus_set(self) -> None:
        self.focus_calls += 1


def _event(widget: Any, char: str = "", keysym: str = "") -> Any:
    return SimpleNamespace(widget=widget, char=char, keysym=keysym)


@contextmanager
def _clock(start: float = 100.0) -> Iterator[list[float]]:
    """Freeze time.monotonic so burst timing is deterministic."""
    state = [start]
    with patch.object(pos_page.time, "monotonic", side_effect=lambda: state[0]):
        yield state


def _bare_pos() -> Any:
    """Construct PosView without Tk: only the scanner redirect is exercised."""
    pos = object.__new__(pos_page.PosView)
    pos.dialog = None
    pos._confirming = False
    pos._last_human_key_ms = 0.0
    pos.scan_buffer = scanner.ScanBuffer()
    pos.scan_debouncer = scanner.ScanDebouncer()
    pos._burst_widget = None
    pos.scan_entry = _FakeEntry()
    pos.added: list[tuple[str, bool]] = []

    def _add_by_barcode(redirect: bool = False) -> None:
        code = pos.scan_entry.get().strip()
        pos.scan_entry.delete(0, "end")
        if redirect and not pos.scan_debouncer.accept(
            code, pos_page.time.monotonic() * 1000.0
        ):
            return
        pos.added.append((code, redirect))

    pos._add_by_barcode = _add_by_barcode
    return pos


class ScannerRedirectTests(unittest.TestCase):
    """SIMULATED USB scanner: fast digits + Enter, focus on the WRONG field."""

    def _burst(self, pos: Any, widget: Any, code: str,
               clock: list[float], step: float = 0.01) -> None:
        # step is in SECONDS; _on_root_key converts monotonic() to ms.
        # 0.01 s = 10 ms per character, typical of a HID scanner.
        for char in code:
            pos._on_root_key(_event(widget, char, char))
            clock[0] += step

    def test_scan_from_another_field_is_redirected_once(self) -> None:
        pos = _bare_pos()
        other = _FakeEntry("100")
        with _clock() as clock:
            self._burst(pos, other, COKE, clock)
            outcome = pos._on_root_key(_event(other, "\r", "Return"))
        self.assertEqual(outcome, "break")
        self.assertEqual(other.get(), "100")  # stray digits removed
        self.assertEqual(pos.added, [(COKE, True)])
        self.assertGreaterEqual(pos.scan_entry.focus_calls, 1)

    def test_manual_slow_typing_is_never_redirected(self) -> None:
        pos = _bare_pos()
        other = _FakeEntry()
        with _clock() as clock:
            self._burst(pos, other, COKE, clock, step=0.2)
            outcome = pos._on_root_key(_event(other, "\r", "Return"))
        self.assertIsNone(outcome)
        self.assertEqual(pos.added, [])

    def test_partial_scan_does_not_look_up_a_product(self) -> None:
        pos = _bare_pos()
        other = _FakeEntry()
        with _clock() as clock:
            self._burst(pos, other, "480", clock)
            outcome = pos._on_root_key(_event(other, "\r", "Return"))
        self.assertIsNone(outcome)
        self.assertEqual(pos.added, [])

    def test_unknown_barcode_still_reaches_the_lookup(self) -> None:
        pos = _bare_pos()
        other = _FakeEntry()
        with _clock() as clock:
            self._burst(pos, other, UNKNOWN, clock)
            pos._on_root_key(_event(other, "\r", "Return"))
        self.assertEqual(pos.added[0][0], UNKNOWN)

    def test_scan_field_focus_is_handled_by_its_own_return_binding(self) -> None:
        pos = _bare_pos()
        inner = pos.scan_entry._entry
        with _clock() as clock:
            self._burst(pos, inner, COKE, clock)
            outcome = pos._on_root_key(_event(inner, "\r", "Return"))
        self.assertIsNone(outcome)
        self.assertEqual(pos.added, [])  # the real binding adds it, not us

    def test_repeat_scans_are_not_debounced_away(self) -> None:
        pos = _bare_pos()
        other = _FakeEntry()
        with _clock() as clock:
            self._burst(pos, other, COKE, clock)
            pos._on_root_key(_event(other, "\r", "Return"))
            clock[0] += 1.0  # 1000 ms later: a deliberate second item
            self._burst(pos, other, COKE, clock)
            pos._on_root_key(_event(other, "\r", "Return"))
        self.assertEqual(len(pos.added), 2)

    def test_a_bouncing_enter_cannot_ring_the_same_scan_twice(self) -> None:
        """One burst + two Enter events must add the product exactly once."""
        pos = _bare_pos()
        other = _FakeEntry()
        with _clock() as clock:
            self._burst(pos, other, COKE, clock)
            pos._on_root_key(_event(other, "\r", "Return"))
            clock[0] += 0.02  # 20 ms later: the trigger bounced
            outcome = pos._on_root_key(_event(other, "\r", "Return"))
        self.assertIsNone(outcome)
        self.assertEqual(len(pos.added), 1)

    def test_modal_dialog_disables_the_scanner_redirect(self) -> None:
        pos = _bare_pos()
        pos.dialog = SimpleNamespace(winfo_exists=lambda: True)
        other = _FakeEntry()
        with _clock() as clock:
            self._burst(pos, other, COKE, clock)
            outcome = pos._on_root_key(_event(other, "\r", "Return"))
        self.assertIsNone(outcome)
        self.assertEqual(pos.added, [])

    def test_confirming_a_sale_disables_the_scanner_redirect(self) -> None:
        pos = _bare_pos()
        pos._confirming = True
        other = _FakeEntry()
        with _clock() as clock:
            self._burst(pos, other, COKE, clock)
            outcome = pos._on_root_key(_event(other, "\r", "Return"))
        self.assertIsNone(outcome)
        self.assertEqual(pos.added, [])

    def test_focus_change_mid_burst_is_not_treated_as_a_scan(self) -> None:
        """Digits from two different widgets must never be concatenated."""
        pos = _bare_pos()
        first, second = _FakeEntry(), _FakeEntry()
        with _clock() as clock:
            for char in "1234":
                pos._on_root_key(_event(first, char, char))
                clock[0] += 0.01
            for char in "567":
                pos._on_root_key(_event(second, char, char))
                clock[0] += 0.01
            outcome = pos._on_root_key(_event(second, "\r", "Return"))
        self.assertIsNone(outcome)
        self.assertEqual(pos.added, [])

    def test_scan_entry_and_inner_entry_both_count_as_scan_focus(self) -> None:
        pos = _bare_pos()
        self.assertTrue(pos._is_scan_focus(pos.scan_entry))
        self.assertTrue(pos._is_scan_focus(pos.scan_entry._entry))
        self.assertFalse(pos._is_scan_focus(_FakeEntry()))

    def test_text_widgets_are_protected_from_the_focus_guard(self) -> None:
        self.assertTrue(pos_page.PosView._is_text_widget(_FakeEntry()))
        self.assertFalse(pos_page.PosView._is_text_widget(None))


# ======================================================================
# 6 - POS transaction / inventory integration (real temporary SQLite)
# ======================================================================
class TransactionIntegrationTests(unittest.TestCase):

    def setUp(self) -> None:
        permissions.clear_session()
        receipt.reset_print_guard()
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "stage14.db"
        database.initialize_database(self.db_path)
        products.ensure_schema(self.db_path)
        products.bootstrap_catalog(self.db_path)

    def tearDown(self) -> None:
        permissions.clear_session()
        receipt.reset_print_guard()
        self.temp.cleanup()

    def _scalar(self, sql: str, params: tuple = ()) -> Any:
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(sql, params).fetchone()[0]
        finally:
            conn.close()

    def _sale(self, cart: Any, tendered: Any = 500) -> dict[str, Any]:
        return store_sales.confirm_sale(
            cart, "Cash", tendered, user_id=1,
            db_path=self.db_path, actor=MANAGER,
        )

    def _stock(self, barcode: str) -> float:
        return float(self._scalar(
            "SELECT quantity_on_hand FROM ingredients WHERE barcode = ?",
            (barcode,),
        ))

    def test_confirm_sale_uses_one_immediate_write_transaction(self) -> None:
        source = _source("store_sales.py")
        start = source.index("def confirm_sale(")
        body = source[start:source.index("def void_sale(", start)]
        self.assertIn("with database.transaction(db_path) as conn:", body)
        transaction_source = _source("database.py")
        self.assertIn("BEGIN IMMEDIATE", transaction_source)

    def test_a_successful_sale_deducts_inventory_and_writes_snapshots(self) -> None:
        before = self._stock(COKE)
        result = self._sale([(COKE, 2)])
        self.assertEqual(result["total_amount"], 70.0)
        self.assertEqual(self._stock(COKE), before - 2)
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM store_sales"), 1)
        self.assertEqual(
            self._scalar("SELECT COUNT(*) FROM store_sale_items"), 1
        )
        self.assertEqual(
            self._scalar("SELECT COUNT(*) FROM stock_out_details"), 1
        )
        self.assertEqual(
            self._scalar("SELECT quantity FROM store_sale_items"), 2
        )

    def test_insufficient_stock_writes_nothing(self) -> None:
        # 999 is above the seeded stock but within the per-line quantity cap,
        # so this exercises the stock check rather than cart validation.
        with self.assertRaisesRegex(ValueError, "Not enough stock"):
            self._sale([(COKE, 999)])
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM store_sales"), 0)
        self.assertEqual(
            self._scalar("SELECT COUNT(*) FROM stock_out_details"), 0
        )

    def test_insufficient_tender_writes_nothing(self) -> None:
        with self.assertRaisesRegex(ValueError, "less than the total"):
            self._sale([(COKE, 1)], 1)
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM store_sales"), 0)

    def test_unknown_barcode_in_a_sale_writes_nothing(self) -> None:
        with self.assertRaises(ValueError):
            self._sale([(UNKNOWN, 1)])
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM store_sales"), 0)

    def test_printer_failure_never_reverses_a_committed_sale(self) -> None:
        result = self._sale([(COKE, 2), (WATER, 1)])
        with patch.object(
            receipt, "print_receipt",
            return_value={"method": "escpos_failed", "ok": False,
                          "detail": "Printer offline. Receipt was archived."},
        ):
            issued = receipt.issue_store_receipt(
                result, base_dir=self.temp.name
            )
        self.assertFalse(issued["print"]["ok"])
        detail = store_sales.get_sale_detail(result["sale_id"], self.db_path)
        self.assertEqual(detail["status"], "completed")
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM store_sales"), 1)
        self.assertEqual(
            self._scalar("SELECT COUNT(*) FROM store_sale_items"), 2
        )
        self.assertTrue(Path(issued["path"]).exists())

    def test_receipt_snapshots_survive_a_later_price_change(self) -> None:
        result = self._sale([(COKE, 1)])
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE ingredients SET unit_price = 999 WHERE barcode = ?",
            (COKE,),
        )
        conn.commit()
        conn.close()
        text = receipt.build_store_receipt(result)
        self.assertIn(format_money(35.0), text)
        self.assertNotIn(format_money(999.0), text)

    def test_scanner_lookup_returns_the_product_for_a_known_barcode(self) -> None:
        match = store_sales.lookup_product(COKE, self.db_path)
        self.assertEqual(match["barcode"], COKE)
        self.assertIn("Coca-Cola", match["name"])

    def test_scanner_lookup_gives_a_clear_error_for_unknown_barcodes(self) -> None:
        with self.assertRaisesRegex(ValueError, "No sellable product found"):
            store_sales.lookup_product(UNKNOWN, self.db_path)

    def test_pos_confirm_guards_against_a_double_click(self) -> None:
        source = _source("pages/store_pos.py")
        start = source.index("def _confirm(self)")
        body = source[start:source.index("def _sale_record", start)]
        self.assertLess(
            body.index("if self._confirming:"),
            body.index("store_sales.confirm_sale("),
        )
        self.assertIn("finally:", body)

    def test_receipt_is_issued_after_confirm_not_inside_the_transaction(self) -> None:
        source = _source("pages/store_pos.py")
        start = source.index("def _confirm(self)")
        body = source[start:source.index("def _sale_record", start)]
        self.assertLess(
            body.index("result = store_sales.confirm_sale("),
            body.index("receipt.issue_store_receipt("),
        )


# ======================================================================
# 7 - Wiring, dependencies and source hygiene
# ======================================================================
class SourceHygieneTests(unittest.TestCase):

    def test_pywin32_is_declared_as_an_optional_windows_extra(self) -> None:
        requirements = _source("requirements.txt")
        self.assertIn("pywin32", requirements.lower())
        self.assertIn('sys_platform == "win32"', requirements)

    def test_no_barcode_imaging_library_is_required(self) -> None:
        requirements = _source("requirements.txt").lower()
        for library in ("zxing", "opencv", "pyzbar", "python-barcode"):
            self.assertNotIn(library, requirements)

    def test_scanner_module_has_no_database_or_gui_dependency(self) -> None:
        source = _source("scanner.py")
        for forbidden in ("sqlite3", "customtkinter", "import products",
                          "import store_sales", "SELECT "):
            self.assertNotIn(forbidden, source)

    def test_new_modules_use_only_the_peso_sign(self) -> None:
        for relative in (
            "receipt.py", "scanner.py", "pages/store_pos.py",
            "tests/test_stage14.py",
        ):
            with self.subTest(relative=relative):
                text = _source(relative)
                self.assertNotIn("\u00a5", text)
                self.assertNotIn("\uffe5", text)

    def test_printer_dialog_enumerates_printers_instead_of_guessing(self) -> None:
        source = _source("pages/store_pos.py")
        self.assertIn("class PrinterDialog", source)
        self.assertIn("receipt.printer_settings()", source)
        self.assertIn("self._open_printer_dialog", source)
        self.assertIn("_install_scanner_focus()", source)

    def test_root_binding_is_released_when_the_page_is_destroyed(self) -> None:
        source = _source("pages/store_pos.py")
        self.assertIn('bind("<Key>"', source)
        self.assertIn('"<Destroy>"', source)
        self.assertIn("after_cancel", source)
        self.assertIn('unbind("<Key>"', source)

    def test_pos_view_does_not_shadow_the_tkinter_internal_root(self) -> None:
        """Regression: assigning self._root breaks every winfo_toplevel()."""
        source = _source("pages/store_pos.py")
        self.assertNotIn("self._root", source)
        self.assertIn("self._scanner_root", source)

    def test_schema_version_and_startup_order_are_unchanged(self) -> None:
        self.assertEqual(database.SCHEMA_VERSION, 4)
        startup = _source("main.py")
        self.assertLess(
            startup.index("database.initialize_database()"),
            startup.index("products.ensure_schema()"),
        )
        self.assertLess(
            startup.index("products.ensure_schema()"),
            startup.index("products.bootstrap_catalog()"),
        )

    def test_no_new_database_tables_were_introduced(self) -> None:
        source = _source("sales_report.py") + _source("receipt.py") + _source(
            "scanner.py"
        )
        self.assertNotIn("CREATE TABLE", source)

    def test_cashier_still_cannot_view_reports_or_manage_products(self) -> None:
        cashier = {"user_id": 2, "username": "cashier", "role": "cashier"}
        self.assertFalse(
            permissions.can(cashier, permissions.CAP_VIEW_REPORTS)
        )
        self.assertFalse(
            permissions.can(cashier, permissions.CAP_MANAGE_PRODUCTS)
        )
        self.assertTrue(
            permissions.can(cashier, permissions.CAP_RECORD_SALE)
        )

    def test_scan_field_has_no_placeholder_that_can_hide_a_scan(self) -> None:
        """Regression: CTkEntry.get() returns "" while placeholder is active.

        A HID scanner can type in the instant the POS window regains focus,
        before the queued FocusIn runs. Those digits were silently lost.
        """
        source = _source("pages/store_pos.py")
        self.assertNotIn(
            'placeholder_text="Scan barcode and press Enter..."', source
        )
        self.assertIn("self._raw_text(self.scan_entry)", source)
        self.assertIn("def _raw_text", source)


class PlaceholderSafetyTests(unittest.TestCase):
    """Scanner input must never trust CTkEntry.get() in placeholder mode."""

    class _PlaceholderEntry:
        def __init__(self, real: str) -> None:
            self._entry = _FakeEntry(real)
            self._placeholder_text = "0.00"

        def get(self) -> str:  # CTkEntry.get() lies in placeholder mode
            return ""

    def test_raw_text_reads_the_inner_entry(self) -> None:
        widget = self._PlaceholderEntry("100" + COKE)
        self.assertEqual(widget.get(), "")
        self.assertEqual(pos_page.PosView._raw_text(widget), "100" + COKE)

    def test_strip_burst_tail_removes_stray_digits_from_the_inner_entry(
        self,
    ) -> None:
        widget = self._PlaceholderEntry("100" + COKE)
        pos_page.PosView._strip_burst_tail(widget, COKE)
        self.assertEqual(widget._entry.get(), "100")

    def test_raw_text_is_safe_for_broken_widgets(self) -> None:
        self.assertEqual(pos_page.PosView._raw_text(None), "")


if __name__ == "__main__":
    unittest.main()
