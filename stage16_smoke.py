"""
Stage 16 GUI smoke test - automatic change ("auto deduct") + quick cash.

Opens the REAL CustomTkinter windows and drives the REAL cashier POS:

    scan 1 x Boy Bawang (PHP 18.00) -> tap the PHP 100 Quick cash button ->
    "Change due: PHP 82.00" appears with a bill breakdown -> confirm ->
    ONE atomic sale with change_amount 82.00, stock -1, archived receipt ->
    everything resets for the next customer. Also proves a Senior/PWD
    discounted basket auto-deducts from the NET total, and that a short
    payment is shown in red and writes no sale.

ALL data goes to a temporary SQLite file and a temporary receipts folder.
database.DB_PATH and receipt.RECEIPTS_DIR are redirected BEFORE any widget is
built. Printing and barcode scanning are SIMULATED (no USB thermal printer or
physical HID scanner is used). The delivered database is opened read-only for
a SHA-256 hash check.

Screenshots go to assets/images/stage16_*.png.
"""

from __future__ import annotations

import ctypes
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

import change
import database
import discounts
import products
import receipt
import users_repository
from components.shell import AppShell
from components.theme import DANGER, SUCCESS, apply_appearance
from formatting import format_money

OUT = ROOT / "assets" / "images"
OUT.mkdir(parents=True, exist_ok=True)
REAL_DB = ROOT / "database" / "ramen_inventory.db"

TEMP = tempfile.TemporaryDirectory()
TMP = Path(TEMP.name)
DB = TMP / "ramen_inventory.db"
RECEIPTS = TMP / "receipts"

REAL_HASH_BEFORE = hashlib.sha256(REAL_DB.read_bytes()).hexdigest()

BOY_BAWANG = "4800010234560"      # Boy Bawang Garlic 50 g - PHP 18.00
PRICE = 18.00
CASH = 100
CHANGE = 82.00

PASS: list[str] = []
FAIL: list[str] = []
CAPTURE_FAILS: list[str] = []

# ---- redirect every write away from the delivered project --------------
database.DB_PATH = DB
receipt.RECEIPTS_DIR = RECEIPTS
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
root.title("Neighborhood Mart - Stage 16 automatic change smoke")
root.geometry("1160x720+40+40")
root.configure(fg_color="#FAF6EF")
root.update()


def check(condition: bool, label: str) -> None:
    (PASS if condition else FAIL).append(label)
    print(("  PASS  " if condition else "  FAIL  ") + label)


def pump(ms: int = 240) -> None:
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


def buttons(widget) -> list:
    found: list = []

    def walk(node) -> None:
        try:
            if isinstance(node, ctk.CTkButton):
                found.append(node)
        except Exception:
            pass
        for child in node.winfo_children():
            walk(child)

    walk(widget)
    return found


def find_button(widget, text: str):
    for button in buttons(widget):
        try:
            if str(button.cget("text")) == text:
                return button
        except Exception:
            pass
    return None


def button_texts(widget) -> list[str]:
    return [str(b.cget("text")) for b in buttons(widget)]


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


def scroll_to(page, fraction: float) -> None:
    """Stage 17: compact layout fits at fraction 0; keep the hook for larger windows."""
    canvas = page.scroller._parent_canvas
    canvas.update_idletasks()
    canvas.yview_moveto(fraction)
    root.update()
    pump(140)


def set_tendered(pos, text: str) -> None:
    pos.tendered_entry.delete(0, "end")
    pos.tendered_entry.insert(0, text)
    root.update()
    pump(80)


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


try:
    # ==================================================================
    # 1 - The Stage 16 widgets exist on the cashier POS
    # ==================================================================
    shell = AppShell(
        root, user=CASHIER,
        on_logout=lambda: None,
        on_change_password=lambda: None,
        on_exit=lambda: None,
    )
    shell.pack(fill="both", expand=True)
    root.update()
    pos = shell.current_page
    pos.auto_print = False
    pos.receipt_dir = RECEIPTS

    check(pos.__class__.__name__ == "PosView",
          "cashier lands directly on the barcode Point of Sale")
    for attr in ("change_label", "breakdown_label", "tendered_var",
                 "tendered_entry"):
        check(hasattr(pos, attr), f"POS has the Stage 16 widget {attr}")
    check(str(pos.change_label.cget("text")) == "Scan items to see the change.",
          "empty basket shows the no-change hint")

    middle = pos.tendered_entry.master
    captions = [change.quick_cash_label(v) for v in change.QUICK_CASH]
    for caption in captions:
        check(caption in button_texts(middle),
              f"Quick cash button {caption} exists")
    hundred = find_button(middle, change.quick_cash_label(100))
    check(hundred is not None, "PHP 100 quick cash button is clickable")
    check("Quick cash - tap the bill you were handed" in all_text(middle),
          "quick cash hint is visible")
    check("Cash received - change is worked out automatically." in all_text(middle),
          "cashier is told the change is automatic")
    shot("stage16_01_cashier_pos_empty.png")

    # ==================================================================
    # 2 - Scan ONE Boy Bawang (PHP 18.00)
    # ==================================================================
    stock_before = stock(BOY_BAWANG)
    manual_entry(pos.scan_entry, BOY_BAWANG)
    check(len(pos.cart) == 1, "one barcode adds one basket line")
    check(pos.total_amount == PRICE,
          "basket total is PHP 18.00 (1 x Boy Bawang)")
    check(str(pos.total_label.cget("text")) == format_money(PRICE),
          "checkout TOTAL shows PHP 18.00")
    check("Tap a bill" in str(pos.change_label.cget("text")),
          "cashier is prompted to tap/enter the cash received")
    scroll_to(pos, 0.0)
    shot("stage16_02_boy_bawang_basket.png")

    # ==================================================================
    # 3 - ONE TAP on PHP 100 => auto-deduct => PHP 82.00 change
    # ==================================================================
    assert hundred is not None
    hundred.invoke()
    root.update()
    pump(150)
    check(pos.tendered_entry.get() == "100",
          "quick cash button fills Amount tendered with 100")
    change_text = str(pos.change_label.cget("text"))
    check(change_text == f"Change due: {format_money(CHANGE)}",
          "change is AUTO-DEDUCTED and shown as PHP 82.00")
    check(str(pos.change_label.cget("text_color")) == SUCCESS,
          "change label is green when cash covers the total")
    detail = str(pos.breakdown_label.cget("text"))
    check("Give back:" in detail, "bill breakdown is shown to the cashier")
    check("1 x \u20b150" in detail and "2 x \u20b11" in detail,
          "breakdown suggests 50 + 20 + 10 + 1 + 1")
    check(change.compute(pos.total_amount, pos.tendered_entry.get())["change"]
          == CHANGE,
          "pure logic agrees with the live GUI label")
    scroll_to(pos, 0.0)
    shot("stage16_03_auto_change_82.png")
    scroll_to(pos, 0.0)

    # ==================================================================
    # 4 - Live recalculation for typing, short payments, and re-taps
    # ==================================================================
    set_tendered(pos, "50")
    check(format_money(32) in str(pos.change_label.cget("text")),
          "typing a different bill instantly recalculates the change")
    set_tendered(pos, "10")
    check("Short by " + format_money(8) in str(pos.change_label.cget("text")),
          "a short payment is flagged in red before Confirm")
    check(str(pos.change_label.cget("text_color")) == DANGER,
          "short payment label is red")
    sales_before = int(scalar("SELECT COUNT(*) FROM store_sales"))
    pos._confirm()
    root.update()
    pump(150)
    check(int(scalar("SELECT COUNT(*) FROM store_sales")) == sales_before,
          "an underpaid Confirm writes NO sale")
    check(stock(BOY_BAWANG) == stock_before,
          "an underpaid Confirm deducts NO stock")

    hundred.invoke()
    root.update()
    pump(120)
    check(pos.tendered_entry.get() == "100",
          "re-tapping PHP 100 restores the correct cash received")
    check(format_money(CHANGE) in str(pos.change_label.cget("text")),
          "change returns to PHP 82.00")

    # ==================================================================
    # 5 - Confirm: ONE sale, correct change, receipt, stock, reset
    # ==================================================================
    receipts_before = len(list(RECEIPTS.glob("*.txt"))) if RECEIPTS.exists() else 0
    pos._confirm()
    root.update()
    pump(220)

    check(int(scalar("SELECT COUNT(*) FROM store_sales")) == sales_before + 1,
          "exactly ONE sale is committed")
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        row = dict(conn.execute(
            "SELECT * FROM store_sales ORDER BY sale_id DESC LIMIT 1"
        ).fetchone())
    finally:
        conn.close()
    check(row["total_amount"] == PRICE, "sale stores the PHP 18.00 total")
    check(row["amount_tendered"] == 100.0, "sale stores PHP 100.00 tendered")
    check(row["change_amount"] == CHANGE,
          "sale stores PHP 82.00 change (100 - 18)")
    check(stock_before - stock(BOY_BAWANG) == 1,
          "exactly ONE Boy Bawang is deducted from stock")
    check(int(scalar("SELECT COUNT(*) FROM store_sale_items")) == 1,
          "exactly ONE immutable sale item is written")

    receipts = sorted(RECEIPTS.glob("*.txt")) if RECEIPTS.exists() else []
    check(len(receipts) == receipts_before + 1,
          "exactly ONE receipt is archived")
    receipt_text = receipts[-1].read_text(encoding="utf-8") if receipts else ""
    for marker in ("Boy Bawang", "Tendered", "Change",
                   format_money(100.0), format_money(CHANGE)):
        check(marker in receipt_text, f"archived receipt contains {marker!r}")
    check(max((len(line) for line in receipt_text.splitlines()), default=0)
          <= receipt.RECEIPT_WIDTH,
          "every receipt line still fits the 58 mm paper")

    check(pos.tendered_entry.get() == "",
          "amount tendered is cleared after the sale")
    check(str(pos.change_label.cget("text")) == "Scan items to see the change.",
          "change label resets for the next customer")
    check(len(pos.cart) == 0, "basket is empty after the sale")

    pos._view_sale(row["sale_id"])
    pump(180)
    dialog = pos.dialog
    check(dialog is not None and bool(dialog.winfo_exists()),
          "receipt dialog opens from Sales history")
    shot("stage16_04_receipt_change.png", target=dialog)
    if dialog is not None:
        dialog.close()
        pump(120)

    # ==================================================================
    # 6 - Auto-deduct uses the NET total on a discounted sale
    # ==================================================================
    manual_entry(pos.scan_entry, BOY_BAWANG)
    pos.discount_combo.set(discounts.LABELS[discounts.SENIOR])
    pos.customer_name_entry.insert(0, "Maria Santos")
    pos.customer_id_entry.insert(0, "SC-2026-00451")
    pos._render_cart()
    root.update()
    check(pos.total_amount == 14.40,
          "20% Senior discount prices the basket at PHP 14.40")
    hundred.invoke()
    root.update()
    pump(120)
    check(format_money(85.60) in str(pos.change_label.cget("text")),
          "auto change uses the NET total (100 - 14.40 = 85.60)")
    check(change.compute(pos.total_amount, "100")["change"] == 85.60,
          "pure logic agrees with the discounted change")
    scroll_to(pos, 0.0)
    shot("stage16_05_discounted_auto_change.png")
    scroll_to(pos, 0.0)
    pos._confirm()
    root.update()
    pump(200)
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        second = dict(conn.execute(
            "SELECT * FROM store_sales ORDER BY sale_id DESC LIMIT 1"
        ).fetchone())
    finally:
        conn.close()
    check(second["discount_type"] == discounts.SENIOR
          and second["change_amount"] == 85.60,
          "discounted sale stores PHP 85.60 change")
    check(int(scalar("SELECT COUNT(*) FROM store_sales")) == sales_before + 2,
          "two sales in total, no duplicate transactions")

    # ==================================================================
    # 7 - Delivery hygiene
    # ==================================================================
    check(hashlib.sha256(REAL_DB.read_bytes()).hexdigest() == REAL_HASH_BEFORE,
          "delivered database is byte-for-byte unchanged")
    check(not (ROOT / "config").exists(),
          "no config/ folder was created in the delivered project")
    check(not (ROOT / "reports").exists(),
          "no reports/ folder was created in the delivered project")
    shots = sorted(OUT.glob("stage16_*.png"))
    check(len(shots) >= 5, f"Stage 16 screenshots captured ({len(shots)})")

except Exception:
    traceback.print_exc()
    FAIL.append("UNEXPECTED EXCEPTION - see traceback above")
finally:
    try:
        root.destroy()
    except Exception:
        pass

print("\n=== STAGE 16 SMOKE SUMMARY ===")
print(f"PASS {len(PASS)}   FAIL {len(FAIL)}")
for label in FAIL:
    print("  FAILED: " + label)
print(f"screenshots: {len(list(OUT.glob('stage16_*.png')))}")
print(f"capture fallbacks: {CAPTURE_FAILS}")
print("REAL HARDWARE TESTS: NOT PERFORMED (simulated scanner + archive-only print).")
TEMP.cleanup()
sys.exit(0 if not FAIL else 1)
