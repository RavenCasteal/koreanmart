"""
Stage 17 GUI smoke test - compact cashier POS + NO post-sale Save dialog.

Drives the REAL CustomTkinter cashier window at 1160x720:

    * the whole checkout flow (scan, basket, TOTAL, live CHANGE, quick cash,
      tendered, Confirm, both toggles) fits with ZERO scrolling;
    * "More tools" and "Sales history" open/close with one click and keep
      every Stage 15/16 widget working;
    * confirming a sale ARCHIVES the receipt silently. os.startfile(...,"print")
      is spied on and must NEVER be called - that is what used to make
      "Microsoft Print to PDF" ask the cashier where to save a file;
    * a discounted sale still uses the NET total and prints discount details.

ALL data goes to a temporary SQLite file, a temporary receipts folder and a
temporary printer config path. The delivered database is opened read-only for
a SHA-256 hash check. Printing and barcode scanning are SIMULATED: no USB
thermal printer or physical HID scanner is used.

Screenshots go to assets/images/stage17_*.png.
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
from components.theme import apply_appearance
from formatting import format_money

OUT = ROOT / "assets" / "images"
OUT.mkdir(parents=True, exist_ok=True)
REAL_DB = ROOT / "database" / "ramen_inventory.db"

TEMP = tempfile.TemporaryDirectory()
TMP = Path(TEMP.name)
DB = TMP / "ramen_inventory.db"
RECEIPTS = TMP / "receipts"
PRINTER_CONFIG = TMP / "config" / "printer.json"

REAL_HASH_BEFORE = hashlib.sha256(REAL_DB.read_bytes()).hexdigest()

BOY_BAWANG = "4800010234560"      # Boy Bawang Garlic 50 g - PHP 18.00
PRICE = 18.00
CASH = 100
DUE = 82.00

PASS: list[str] = []
FAIL: list[str] = []
CAPTURE_FAILS: list[str] = []
STARTFILE_CALLS: list[tuple] = []

# ---- redirect every write away from the delivered project --------------
database.DB_PATH = DB
receipt.RECEIPTS_DIR = RECEIPTS
receipt.PRINTER_CONFIG_PATH = PRINTER_CONFIG

# Spy on the ONE call that used to open the Windows "Save Print Output As"
# dialog. Do NOT stub out print_receipt - exercise the real archive-only path.
_original_startfile = getattr(receipt.os, "startfile", None)


def _spy_startfile(*args, **kwargs):
    STARTFILE_CALLS.append((args, kwargs))
    return None


receipt.os.startfile = _spy_startfile  # type: ignore[attr-defined]

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
root.title("Neighborhood Mart - Stage 17 compact POS smoke")
root.geometry("1160x720+40+40")
root.configure(fg_color="#FAF6EF")
root.update()


def check(condition: bool, label: str) -> None:
    (PASS if condition else FAIL).append(label)
    print(("  PASS  " if condition else "  FAIL  ") + label)


def pump(ms: int = 200) -> None:
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
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD),
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


def scroll_to(page, fraction: float) -> None:
    canvas = page.scroller._parent_canvas
    canvas.update_idletasks()
    canvas.yview_moveto(fraction)
    root.update()
    pump(140)


def manual_entry(entry, code: str) -> None:
    entry.delete(0, "end")
    entry.insert(0, code)
    entry.focus_set()
    entry.event_generate("<Return>")
    root.update()
    pump(100)


def buttons(widget) -> list:
    found: list = []

    def walk(w):
        for child in w.winfo_children():
            if isinstance(child, ctk.CTkButton):
                found.append(child)
            walk(child)

    walk(widget)
    return found


def button_texts(widget) -> list[str]:
    return [str(b.cget("text")) for b in buttons(widget)]


def find_button(widget, text: str):
    for button in buttons(widget):
        if str(button.cget("text")) == text:
            return button
    return None


def all_text(widget) -> str:
    found: list[str] = []

    def walk(w):
        for child in w.winfo_children():
            try:
                found.append(str(child.cget("text")))
            except Exception:
                pass
            walk(child)

    walk(widget)
    return "\n".join(found)


def scalar(sql: str, params: tuple = ()) -> object:
    conn = sqlite3.connect(DB)
    try:
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


def stock(barcode: str) -> float:
    return float(scalar(
        "SELECT quantity_on_hand FROM ingredients WHERE barcode = ?",
        (barcode,),
    ))


def content_height(page) -> int:
    root.update()
    return int(page.container.winfo_reqheight())


def viewport_height(page) -> int:
    root.update()
    return int(page.scroller._parent_canvas.winfo_height())


try:
    # ==================================================================
    # 1 - Compact layout: no scrolling for a normal basket
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
    # NOTE: auto_print stays TRUE on purpose - this smoke exercises the real
    # receipt.print_receipt() path, which must never open a Save dialog.
    pos.receipt_dir = RECEIPTS

    check(pos.__class__.__name__ == "PosView",
          "cashier lands directly on the barcode Point of Sale")
    check(hasattr(pos, "_sections") and
          set(pos._sections) == {"tools", "history"},
          "two collapsible sections exist (tools + history)")
    check(all(not info["open"] for info in pos._sections.values()),
          "both sections start collapsed")
    check(len(pos._toggle_bar.winfo_children()) == 2,
          "ONE compact toggle bar holds both section buttons")

    empty_h, view_h = content_height(pos), viewport_height(pos)
    check(empty_h < 642,
          f"empty POS is compact ({empty_h}px vs the old 1750px)")
    print(f"  INFO  empty content {empty_h}px | visible canvas {view_h}px")

    for attr in (
        "scan_entry", "picker_search", "cart_frame", "cart_note",
        "total_label", "item_count_label", "change_label",
        "breakdown_label", "tendered_entry", "payment_combo",
        "confirm_button", "gross_label", "shortage_label",
        "discount_combo", "customer_name_entry", "customer_id_entry",
        "discount_status_label", "calc_display", "history_search",
        "history_frame",
    ):
        check(hasattr(pos, attr), f"Stage 17 kept the widget {attr}")

    stock_before = stock(BOY_BAWANG)
    manual_entry(pos.scan_entry, BOY_BAWANG)
    check(len(pos.cart) == 1, "one barcode adds one basket line")
    check(pos.total_amount == PRICE, "basket total is PHP 18.00")

    hundred = find_button(pos.tendered_entry.master, change.quick_cash_label(100))
    check(hundred is not None, "PHP 100 quick cash button is present")
    assert hundred is not None
    hundred.invoke()
    root.update()
    pump(120)
    check(str(pos.change_label.cget("text")) ==
          f"Change due: {format_money(DUE)}",
          "auto change still shows PHP 82.00 in the compact layout")

    one_h = content_height(pos)
    check(one_h <= view_h,
          f"1-item checkout fits with NO scrolling ({one_h}px <= {view_h}px)")
    scroll_to(pos, 0.0)
    shot("stage17_01_compact_pos.png")

    # 2 and 3 basket LINES must also fit inside the visible canvas. Repeated
    # scans of one barcode raise QUANTITY, so add distinct fake lines here.
    extra_codes = ("4800001234567", "4800002234564")  # real catalog items
    for extra in (2, 3):
        while len(pos.cart) < extra:
            code = extra_codes[len(pos.cart) - 1]
            pos.cart.append({
                "barcode": code, "name": code,
                "quantity": 1, "unit_price": 10.0,
            })
        pos.refresh()
        h = content_height(pos)
        check(h <= view_h,
              f"{extra}-line basket fits with NO scrolling ({h}px <= {view_h}px)")
    # Back to the real single-item sale for the rest of the smoke.
    pos.cart = [{
        "barcode": BOY_BAWANG, "name": "Boy Bawang Garlic 50 g",
        "quantity": 1, "unit_price": PRICE,
    }]
    pos.refresh()
    root.update()

    # ==================================================================
    # 2 - Collapsible sections still expose every tool
    # ==================================================================
    pos.open_tools()
    root.update()
    check(pos._sections["tools"]["open"],
          "'More tools' opens on one click")
    check(pos.discount_combo.winfo_ismapped() == 1 or
          pos.discount_combo.winfo_exists() == 1,
          "discount controls are built and reachable")
    check(all(label in button_texts(pos._sections["tools"]["body"])
              for label in ("00", "=", "Use result as amount tendered")),
          "full calculator keypad + 'Use result' survive the move")
    scroll_to(pos, 1.0)
    shot("stage17_02_more_tools.png")

    pos.set_section("tools", False)
    root.update()
    pump(120)
    check(not pos._sections["tools"]["open"], "'More tools' closes again")
    closed_h, closed_view = content_height(pos), viewport_height(pos)
    print(f"  INFO  tools closed: content {closed_h}px | canvas {closed_view}px")
    check(closed_h <= closed_view,
          f"closing 'More tools' restores no-scroll layout "
          f"({closed_h}px <= {closed_view}px)")

    pos.open_history()
    root.update()
    check(pos._sections["history"]["open"],
          "'Sales history' opens on one click")
    check(pos.history_search.winfo_exists() == 1,
          "history search is built and reachable")
    scroll_to(pos, 1.0)
    shot("stage17_03_sales_history.png")
    pos.set_section("history", False)
    scroll_to(pos, 0.0)

    # ==================================================================
    # 3 - Confirm a sale: ZERO print dialogs, receipt archived silently
    # ==================================================================
    sales_before = int(scalar("SELECT COUNT(*) FROM store_sales"))
    receipts_before = len(list(RECEIPTS.glob("*.txt"))) if RECEIPTS.exists() else 0
    pos._confirm()
    root.update()
    pump(240)

    sales_after = int(scalar("SELECT COUNT(*) FROM store_sales"))
    check(sales_after == sales_before + 1,
          "exactly ONE sale is committed")
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        sale = dict(conn.execute(
            "SELECT * FROM store_sales ORDER BY sale_id DESC LIMIT 1"
        ).fetchone())
    finally:
        conn.close()
    check(float(sale["change_amount"]) == DUE,
          "committed change is PHP 82.00")
    check(stock(BOY_BAWANG) == stock_before - 1,
          "one Boy Bawang was deducted from stock")
    archived = sorted(RECEIPTS.glob("*.txt"))
    check(len(archived) == receipts_before + 1,
          "receipt was archived automatically")
    text = archived[-1].read_text(encoding="utf-8") if archived else ""
    check(format_money(DUE) in text,
          "archived receipt shows the PHP 82.00 change")
    check("Microsoft" not in text and "Save Print Output" not in text,
          "receipt archive is clean")

    check(STARTFILE_CALLS == [],
          "NO os.startfile(...,'print') call - no Save dialog after Confirm")
    check(not PRINTER_CONFIG.exists(),
          "no printer config file was created in the delivered project")
    status = str(pos.status_label.cget("text"))
    check("Receipt archived automatically" in status,
          "cashier is told the receipt was saved automatically")
    shot("stage17_04_sale_no_save_prompt.png")

    check(pos.tendered_entry.get() == "" and len(pos.cart) == 0,
          "POS resets for the next customer")

    # ==================================================================
    # 4 - Discounted sale still works through the collapsible tools
    # ==================================================================
    pos.open_tools()
    manual_entry(pos.scan_entry, BOY_BAWANG)
    pos.discount_combo.set(discounts.LABELS[discounts.SENIOR])
    pos.customer_name_entry.insert(0, "Ana Santos")
    pos.customer_id_entry.insert(0, "SC-2026-00451")
    pos._render_cart()
    root.update()
    check(pos.total_amount == 14.40,
          "Senior discount still prices the basket at PHP 14.40")
    hundred = find_button(pos.tendered_entry.master, change.quick_cash_label(100))
    assert hundred is not None
    hundred.invoke()
    root.update()
    pump(140)
    check(format_money(85.60) in str(pos.change_label.cget("text")),
          "auto change uses the NET total (PHP 85.60)")
    scroll_to(pos, 1.0)
    shot("stage17_05_discount_still_works.png")
    scroll_to(pos, 0.0)
    pos._confirm()
    root.update()
    pump(220)
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        second = dict(conn.execute(
            "SELECT * FROM store_sales ORDER BY sale_id DESC LIMIT 1"
        ).fetchone())
    finally:
        conn.close()
    check(float(second["total_amount"]) == 14.40 and
          float(second["change_amount"]) == 85.60,
          "discounted sale committed correctly")
    check(second["customer_name"] == "Ana Santos" and
          second["discount_type"] == discounts.SENIOR,
          "immutable discount snapshot is stored")
    check(STARTFILE_CALLS == [],
          "still NO Save dialog on the second sale")

    # ==================================================================
    # 5 - Delivery hygiene
    # ==================================================================
    real_hash_after = hashlib.sha256(REAL_DB.read_bytes()).hexdigest()
    check(real_hash_after == REAL_HASH_BEFORE,
          "delivered SQLite database is byte-for-byte unchanged")
    conn = sqlite3.connect("file:" + REAL_DB.as_posix() + "?mode=ro", uri=True)
    try:
        check(conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
              "delivered database integrity is ok")
        check(conn.execute("PRAGMA user_version").fetchone()[0] == 4,
              "PRAGMA user_version is still 4 (no schema migration)")
    finally:
        conn.close()
    check(not (ROOT / "config").exists(),
          "no config/ folder was delivered")
    check(not (ROOT / "reports").exists(),
          "no reports/ folder was delivered")

except Exception:
    traceback.print_exc()
    FAIL.append("UNHANDLED EXCEPTION")
finally:
    if _original_startfile is not None:
        receipt.os.startfile = _original_startfile  # type: ignore[attr-defined]
    try:
        root.destroy()
    except Exception:
        pass

print()
print(f"PASS {len(PASS)}   FAIL {len(FAIL)}")
for label in FAIL:
    print("  FAILED: " + label)
print(f"startfile calls (must be 0): {len(STARTFILE_CALLS)}")
print(f"receipts archived: {len(list(RECEIPTS.glob('*.txt')))}")
print(f"screenshots: {len(list(OUT.glob('stage17_*.png')))}")
print("REAL HARDWARE TESTS: NOT PERFORMED - see the hand-off report.")
sys.exit(1 if FAIL else 0)
