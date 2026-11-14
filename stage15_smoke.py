"""
Stage 15 GUI smoke test - Senior / PWD discount + embedded cashier calculator.

Opens the REAL CustomTkinter windows and drives the real pages:

  cashier POS : scan three items -> select a Senior Citizen discount ->
                name + ID are required -> 20% is shown on the basket ->
                use the calculator to work out the amount tendered ->
                confirm the sale -> receipt with discount details ->
                discount does NOT leak to the next (plain) sale
  manager     : Sales Reports shows a Discounts KPI, the Z-read contains the
                SC/PWD discount section, and the CSV has the new columns

ALL data goes to a temporary SQLite file and temporary receipts/reports
folders. database.DB_PATH, receipt.RECEIPTS_DIR and sales_report.REPORTS_DIR
are redirected BEFORE any widget is built. Printing is simulated (no thermal
printer or scanner hardware is used); receipts and Z-reads are still archived.
The delivered database is opened read-only for a SHA-256 hash check.

Screenshots go to assets/images/stage15_*.png.
"""

from __future__ import annotations

import ctypes
import csv
import hashlib
import sqlite3
import sys
import tempfile
import time
import traceback
from ctypes import wintypes
from pathlib import Path

import customtkinter as ctk
from PIL import Image, ImageGrab

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import calculator
import database
import discounts
import permissions
import products
import receipt
import sales_report
import store_sales
import users_repository
from components.shell import AppShell
from components.theme import apply_appearance
from formatting import format_money

OUT = ROOT / "assets" / "images"
OUT.mkdir(parents=True, exist_ok=True)
REAL_DB = ROOT / "database" / "ramen_inventory.db"

TEMP = tempfile.TemporaryDirectory()
TMP = Path(TEMP.name)
DB = TMP / "ramen_inventory.db"
RECEIPTS = TMP / "receipts"
REPORTS = TMP / "reports"

REAL_HASH_BEFORE = hashlib.sha256(REAL_DB.read_bytes()).hexdigest()

COKE = "4800001234567"
WATER = "4800002234564"
NAME = "Maria Santos"
ID_NUMBER = "SC-2026-00451"
SUBTOTAL = 85.0
DISCOUNT = 17.0
NET = 68.0

PASS: list[str] = []
FAIL: list[str] = []
CAPTURE_FAILS: list[str] = []

# ---- redirect every write away from the delivered project --------------
database.DB_PATH = DB
receipt.RECEIPTS_DIR = RECEIPTS
sales_report.REPORTS_DIR = REPORTS
receipt.print_receipt = lambda text, path=None, printer_name=None: {
    "method": "smoke_test",
    "detail": "Smoke test: archived, printer not used.",
    "ok": True,
}

# ---- exactly what main.py does on first launch --------------------------
database.initialize_database(DB)
products.ensure_schema(DB)
products.bootstrap_catalog(DB)

MANAGER = database.verify_credentials(
    database.DEFAULT_ADMIN_USERNAME, database.DEFAULT_ADMIN_PASSWORD, DB
)
assert MANAGER is not None
users_repository.create_user(
    "counter", "Counter2026!", "Counter Staff", "cashier",
    db_path=DB, actor=MANAGER,
)
CASHIER = database.verify_credentials("counter", "Counter2026!", DB)
assert CASHIER is not None

apply_appearance()
root = ctk.CTk()
root.title("Neighborhood Mart - Stage 15 discount / calculator smoke")
root.geometry("1160x720+40+40")
root.configure(fg_color="#FAF6EF")
root.update()


def check(condition: bool, label: str) -> None:
    (PASS if condition else FAIL).append(label)
    print(("  PASS  " if condition else "  FAIL  ") + label)


def pump(ms: int = 260) -> None:
    end = time.time() + ms / 1000
    while time.time() < end:
        root.update()
        time.sleep(0.02)


def front(win, others=()) -> None:
    for other in others:
        try:
            other.attributes("-topmost", False)
        except Exception:
            pass
    try:
        win.deiconify()
        win.lift()
        win.attributes("-topmost", True)
        win.focus_force()
        ctypes.windll.user32.SetForegroundWindow(win.winfo_id())
    except Exception:
        pass
    win.update_idletasks()
    win.update()


def looks_like_app(image) -> bool:
    small = image.convert("RGB").resize((80, 50))
    light = 0
    for r, g, b in small.getdata():
        if r > 225 and g > 218 and b > 205:
            light += 1
    return light / (80 * 50) > 0.25


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


def grab_window(win) -> object:
    """Capture a Tk toplevel directly with PrintWindow (works headless)."""
    user = ctypes.windll.user32
    gdi = ctypes.windll.gdi32
    hwnd = user.GetAncestor(win.winfo_id(), 2)
    rect = wintypes.RECT()
    user.GetWindowRect(hwnd, ctypes.byref(rect))
    width, height = rect.right - rect.left, rect.bottom - rect.top
    hdc = user.GetWindowDC(hwnd)
    mem_dc = gdi.CreateCompatibleDC(hdc)
    bitmap = gdi.CreateCompatibleBitmap(hdc, width, height)
    old = gdi.SelectObject(mem_dc, bitmap)
    try:
        user.PrintWindow(hwnd, mem_dc, 2)
        header = _BitmapInfoHeader()
        header.biSize = ctypes.sizeof(header)
        header.biWidth = width
        header.biHeight = -height
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = 0
        buffer = ctypes.create_string_buffer(width * height * 4)
        gdi.GetDIBits(mem_dc, bitmap, 0, height, buffer,
                      ctypes.byref(header), 0)
        return Image.frombuffer(
            "RGB", (width, height), buffer, "raw", "BGRX", 0, 1
        ).copy()
    finally:
        gdi.SelectObject(mem_dc, old)
        gdi.DeleteObject(bitmap)
        gdi.DeleteDC(mem_dc)
        user.ReleaseDC(hwnd, hdc)


def shot(name: str, target=None) -> None:
    """Capture WITHOUT stealing focus (PrintWindow renders an occluded window)."""
    win = target or root
    win.update_idletasks()
    win.update()
    image = grab_window(win)
    if not looks_like_app(image):
        front(win, others=[root] if win is not root else ())
        time.sleep(0.28)
        win.update()
        x, y = win.winfo_rootx(), win.winfo_rooty()
        w, h = win.winfo_width(), win.winfo_height()
        image = ImageGrab.grab(bbox=(x, y, x + w, y + h))
    if not looks_like_app(image):
        CAPTURE_FAILS.append(name)
    image.save(OUT / name)
    print(f"  SHOT  {name}  {image.size}")


def all_text(widget) -> str:
    parts: list[str] = []

    def walk(node) -> None:
        try:
            parts.append(str(node.cget("text")))
        except Exception:
            pass
        for child in node.winfo_children():
            walk(child)

    walk(widget)
    return "\n".join(parts)


def button_texts(widget) -> list[str]:
    found: list[str] = []

    def walk(node) -> None:
        try:
            if isinstance(node, ctk.CTkButton):
                found.append(str(node.cget("text")))
        except Exception:
            pass
        for child in node.winfo_children():
            walk(child)

    walk(widget)
    return found


def scroll_to(page, fraction: float) -> None:
    """Move the real CTkScrollableFrame canvas so the extras row is visible."""
    canvas = page.scroller._parent_canvas
    canvas.update_idletasks()
    canvas.yview_moveto(fraction)
    root.update()
    pump(120)


def scalar(sql: str, params: tuple = ()) -> object:
    conn = sqlite3.connect(DB)
    try:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def stock(barcode: str) -> float:
    return float(scalar(
        "SELECT quantity_on_hand FROM ingredients WHERE barcode = ?",
        (barcode,),
    ))


def manual_entry(widget, code: str) -> None:
    """Manual barcode entry (no keyboard-wedge burst): type + Enter."""
    entry = getattr(widget, "_entry", widget)
    try:
        root.focus_force()
    except Exception:
        pass
    entry.focus_set()
    root.update()
    widget.delete(0, "end")
    widget.insert(0, code)
    entry.focus_set()
    entry.event_generate("<Return>")
    root.update()
    pump(100)


def calc_keys(page, *keys: str) -> None:
    """Click the embedded keypad buttons through their bound commands."""
    for key in keys:
        page._calc_press(key)
    root.update()


def build_shell(user):
    shell = AppShell(
        root, user=user,
        on_logout=lambda: None,
        on_change_password=lambda: None,
        on_exit=lambda: None,
    )
    shell.pack(fill="both", expand=True)
    root.update()
    return shell


try:
    # ==================================================================
    # 1 - Cashier POS: the discount card and calculator exist
    # ==================================================================
    shell = build_shell(CASHIER)
    pos = shell.current_page
    pos.auto_print = False
    pos.receipt_dir = RECEIPTS

    check(pos.__class__.__name__ == "PosView",
          "cashier lands directly on the barcode Point of Sale")
    for attr in ("discount_combo", "customer_name_entry",
                 "customer_id_entry", "discount_status_label",
                 "calc_display", "calc"):
        check(hasattr(pos, attr), f"POS has the Stage 15 widget {attr}")
    check(isinstance(pos.calc, calculator.Calculator),
          "POS owns a real calculator.Calculator state machine")
    check(pos.discount_combo.cget("values") == list(discounts.CHOICES)
          or tuple(pos.discount_combo.cget("values")) == discounts.CHOICES,
          "discount selector offers No discount / Senior Citizen / PWD")
    calc_card = pos.calc_display.master
    keys = button_texts(calc_card)
    for key in ("C", "Del", "00", ".", "%", "=", "+", "-", "x", "/"):
        check(key in keys, f"calculator keypad has the '{key}' button")
    check("Use result as amount tendered" in keys,
          "calculator can send its result into Amount tendered")
    check("Senior Citizen / PWD discount" in all_text(pos),
          "discount card is visible on the cashier POS")
    check("Cashier calculator" in all_text(pos),
          "calculator card is visible on the cashier POS")
    shot("stage15_01_cashier_pos.png")

    # ==================================================================
    # 2 - Basket with NO discount
    # ==================================================================
    stock_coke_before = stock(COKE)
    stock_water_before = stock(WATER)
    manual_entry(pos.scan_entry, COKE)
    manual_entry(pos.scan_entry, COKE)
    manual_entry(pos.scan_entry, WATER)
    check(len(pos.cart) == 2, "three manual barcode entries produce 2 lines")
    check(pos.cart[0]["quantity"] == 2,
          "repeated barcode raises the quantity to 2")
    check(pos.total_amount == SUBTOTAL,
          "plain basket total is PHP 85.00 (no discount yet)")
    check("No discount" in str(pos.discount_status_label.cget("text")),
          "discount status says no discount is applied")

    # ==================================================================
    # 3 - Selecting Senior Citizen requires name + ID
    # ==================================================================
    # Stage 17: the discount/calculator cards now live behind ONE toggle.
    # Open it before the screenshots so they still show the real widgets.
    pos.open_tools()
    root.update()
    pos.discount_combo.set(discounts.LABELS[discounts.SENIOR])
    pos._render_cart()
    root.update()
    check(pos.total_amount == NET,
          "20% discount immediately prices the basket at PHP 68.00")
    check(not pos.can_confirm,
          "Confirm is blocked until name and ID number are entered")
    check(str(pos.confirm_button.cget("state")) == "disabled",
          "Confirm button is visibly disabled without customer details")
    check("Enter the customer name" in str(pos.discount_status_label.cget("text")),
          "cashier is told to enter the name and ID number")
    scroll_to(pos, 1.0)
    shot("stage15_02_discount_missing_details.png")

    pos.customer_name_entry.insert(0, NAME)
    pos.customer_id_entry.insert(0, ID_NUMBER)
    pos._render_cart()
    root.update()
    check(pos.can_confirm and str(pos.confirm_button.cget("state")) == "normal",
          "Confirm is enabled once name + ID are filled in")
    status = str(pos.discount_status_label.cget("text"))
    check("-" + format_money(DISCOUNT) in status,
          "discount status shows the exact 20% amount removed")
    check(format_money(SUBTOTAL) in status and format_money(NET) in status,
          "discount status shows both the subtotal and the net payable")
    check(str(pos.total_label.cget("text")) == format_money(NET),
          "checkout TOTAL label shows the NET amount")

    # ==================================================================
    # 4 - Calculator: 100 - 32 = 68 -> amount tendered
    # ==================================================================
    calc_keys(pos, "1", "0", "0", "-", "3", "2", "=")
    check(str(pos.calc_display.cget("text")) == "68",
          "embedded calculator computes 100 - 32 = 68")
    check(pos.calc.value == NET,
          "calculator result equals the discounted net total")
    pos._use_calc_result()
    root.update()
    check(pos.tendered_entry.get() == "68",
          "'Use result as amount tendered' fills the tendered field")
    shot("stage15_03_discount_and_calculator.png")

    # Stage 17: with the compact layout the two cards sit at the BOTTOM of
    # the page when the "More tools" section is open.
    scroll_to(pos, 1.0)
    shot("stage15_04_discount_calculator_scrolled.png")
    pos.set_section("tools", False)
    scroll_to(pos, 0.0)

    # ==================================================================
    # 5 - Confirm the discounted sale
    # ==================================================================
    sales_before = int(scalar("SELECT COUNT(*) FROM store_sales"))
    receipts_before = len(list(RECEIPTS.glob("*.txt"))) if RECEIPTS.exists() else 0
    pos._confirm()
    root.update()
    pump(200)

    sales_after = int(scalar("SELECT COUNT(*) FROM store_sales"))
    check(sales_after == sales_before + 1,
          "exactly ONE sale is committed (no duplicate on confirm)")
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        row = dict(conn.execute(
            "SELECT * FROM store_sales ORDER BY sale_id DESC LIMIT 1"
        ).fetchone())
    finally:
        conn.close()
    check(row["discount_type"] == discounts.SENIOR,
          "committed sale stores the Senior Citizen discount type")
    check(row["subtotal_amount"] == SUBTOTAL
          and row["discount_amount"] == DISCOUNT
          and row["total_amount"] == NET,
          "committed sale stores subtotal, discount and net total")
    check(row["discount_rate"] == 0.20,
          "committed sale stores the immutable 20% rate")
    check(row["customer_name"] == NAME and row["customer_id"] == ID_NUMBER,
          "committed sale stores the customer name and ID number")
    check(row["change_amount"] == 0.0
          and row["amount_tendered"] == NET,
          "tendered amount and change use the NET total")
    check(stock_coke_before - stock(COKE) == 2,
          "discount does not change the Coca-Cola stock deduction")
    check(stock_water_before - stock(WATER) == 1,
          "discount does not change the water stock deduction")

    receipts = sorted(RECEIPTS.glob("*.txt")) if RECEIPTS.exists() else []
    check(len(receipts) == receipts_before + 1,
          "exactly ONE receipt is archived after the sale")
    receipt_text = receipts[-1].read_text(encoding="utf-8") if receipts else ""
    for marker in ("Subtotal", "Senior 20%", "-" + format_money(DISCOUNT),
                   "DISCOUNT DETAILS", NAME, ID_NUMBER,
                   format_money(NET)):
        check(marker in receipt_text, f"archived receipt contains {marker!r}")
    check(max((len(line) for line in receipt_text.splitlines()), default=0)
          <= receipt.RECEIPT_WIDTH,
          "every discounted receipt line fits the 58 mm paper")

    # ---- Post-sale reset: no leak to the next customer ----------------
    check(pos.discount_combo.get() == discounts.LABELS[discounts.NO_DISCOUNT],
          "discount selector resets to No discount after a sale")
    check(pos.customer_name_entry.get() == ""
          and pos.customer_id_entry.get() == "",
          "customer name and ID are cleared after a sale")
    check(str(pos.calc_display.cget("text")) == "0" and pos.calc.value == 0.0,
          "calculator is cleared after a sale")
    check(pos.tendered_entry.get() == "",
          "amount tendered is cleared after a sale")
    check(len(pos.cart) == 0, "basket is empty after a sale")

    # ---- Receipt dialog (reprint) ------------------------------------
    pos._view_sale(row["sale_id"])
    pump(180)
    dialog = pos.dialog
    check(dialog is not None and bool(dialog.winfo_exists()),
          "receipt dialog opens from Sales history")
    shot("stage15_05_receipt_discount_details.png", target=dialog)
    if dialog is not None:
        dialog.close()
        pump(120)

    # ==================================================================
    # 6 - A second (plain) sale proves the discount did not leak
    # ==================================================================
    manual_entry(pos.scan_entry, COKE)
    pos.tendered_entry.insert(0, "100")
    pos._render_cart()
    check(pos.total_amount == 35.0,
          "next customer's basket has no 20% discount")
    pos._confirm()
    root.update()
    pump(180)
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        second = dict(conn.execute(
            "SELECT * FROM store_sales ORDER BY sale_id DESC LIMIT 1"
        ).fetchone())
    finally:
        conn.close()
    check(second["discount_type"] == "" and second["discount_amount"] == 0.0,
          "second sale has NO discount (nothing leaked between customers)")
    check(second["total_amount"] == 35.0 and second["subtotal_amount"] == 35.0,
          "second sale charges the full PHP 35.00")
    check(int(scalar("SELECT COUNT(*) FROM store_sales")) == sales_after + 1,
          "two sales in total, no duplicate transactions")

    # ==================================================================
    # 7 - Manager Sales Reports, Z-read and CSV
    # ==================================================================
    shell.destroy()
    root.update()
    shell = build_shell(MANAGER)
    shell.navigate("reports")
    root.update()
    pump(220)
    reports = shell.current_page
    reports.auto_print = False
    reports.report_dir = REPORTS

    check(reports.__class__.__name__ == "ReportsView",
          "manager can open the Sales Reports page")
    text = all_text(reports)
    check("Discounts" in text, "Reports page shows the new Discounts KPI")
    check("Before discounts" in text and "After discounts" in text,
          "gross / net labels clarify that discounts are excluded / included")
    summary = sales_report.daily_summary(db_path=DB, actor=MANAGER)
    check(summary["gross_sales"] == 120.0,
          "gross sales are the PRE-discount subtotals (85 + 35 = 120)")
    check(summary["discount_amount"] == DISCOUNT
          and summary["discount_count"] == 1,
          "daily summary counts exactly one Senior/PWD discount of PHP 17.00")
    check(summary["net_sales"] == 103.0,
          "net sales subtract the discount (120 - 17 = 103)")
    shot("stage15_06_manager_reports_discounts.png")

    z_text = sales_report.build_z_read(summary)
    for marker in ("SC/PWD discounts", "-" + format_money(DISCOUNT),
                   "SENIOR / PWD DISCOUNTS", "Senior Citizen",
                   "PAYMENT MIX", "NET SALES", format_money(103.0)):
        check(marker in z_text, f"Z-read contains {marker!r}")
    reports._print_z()
    reports._export_csv()
    root.update()
    pump(150)

    z_files = list(REPORTS.glob("Z-READ-*.txt"))
    csv_files = list(REPORTS.glob("sales-*.csv"))
    check(len(z_files) == 1, "one Z-read is archived")
    check(len(csv_files) == 1, "one transactions CSV is exported")
    if csv_files:
        with csv_files[0].open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle))
        check(len(rows[0]) == 13, "CSV has the 5 appended discount columns")
        check(rows[0][8] == "Subtotal (PHP)"
              and rows[0][10] == "Discount (PHP)",
              "CSV headers use the PHP currency code")
        discounted_row = [r for r in rows[1:] if r[9] == "Senior Citizen"]
        check(len(discounted_row) == 1,
              "CSV contains exactly one Senior Citizen sale")
        if discounted_row:
            check(discounted_row[0][8] == "85.00"
                  and discounted_row[0][10] == "17.00"
                  and discounted_row[0][11] == NAME
                  and discounted_row[0][12] == ID_NUMBER,
                  "CSV row holds subtotal, discount, customer name and ID")
        check(any(r[9] == "No discount" for r in rows[1:]),
              "plain sale is labelled No discount in the CSV")
    shot("stage15_07_reports_after_zread_export.png")

    # ==================================================================
    # 8 - Delivery hygiene
    # ==================================================================
    REAL_HASH_AFTER = hashlib.sha256(REAL_DB.read_bytes()).hexdigest()
    check(REAL_HASH_BEFORE == REAL_HASH_AFTER,
          "delivered database is byte-for-byte unchanged")
    check(not (ROOT / "config").exists(),
          "no printer config folder was created in the delivered project")
    check(not (ROOT / "reports").exists(),
          "no reports folder was created in the delivered project")

except Exception:
    FAIL.append("UNEXPECTED EXCEPTION")
    traceback.print_exc()
finally:
    print("\n" + "=" * 62)
    print(f"PASS {len(PASS)}   FAIL {len(FAIL)}")
    for label in FAIL:
        print("  FAILED: " + label)
    print(f"screenshots: {len(list(OUT.glob('stage15_*.png')))}")
    if CAPTURE_FAILS:
        print(f"capture fallbacks: {CAPTURE_FAILS}")
    print("=" * 62)
    try:
        root.destroy()
    except Exception:
        pass
    TEMP.cleanup()
    sys.exit(1 if FAIL else 0)
