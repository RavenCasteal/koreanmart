"""
Stage 14 GUI smoke test - SIMULATED USB scanner and thermal printer.

IMPORTANT: this is NOT a hardware test. No physical barcode scanner or USB
receipt printer is used. Instead:

  * A USB HID scanner is simulated with REAL Tk keyboard events
    (<KeyPress-N> for every digit, then <Return>). These travel through the
    same window-level <Key> binding, scanner.ScanBuffer, product lookup and
    basket code that a real keyboard-wedge scanner drives.
  * win32print is replaced by a fake driver injected into sys.modules. The
    real ESC/POS byte stream, printer discovery, offline detection and RAW
    StartDocPrinter / WritePrinter calls all run against that fake.
  * "Disconnecting" the printer means removing its name from EnumPrinters.

For real hardware verification see the setup / hardware test steps in the
Stage 14 hand-off report.

ALL data goes to a temporary SQLite file: database.DB_PATH is redirected
before any widget is built. Receipts, printer config and screenshots are
the only files written, and config/receipts are redirected to the temp
folder too. The delivered database is opened read-only for a hash check.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
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

import database
import permissions
import products
import receipt
import store_sales
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
CONFIG = TMP / "config" / "printer.json"

REAL_HASH_BEFORE = hashlib.sha256(REAL_DB.read_bytes()).hexdigest()
PRINTER_NAME = "POS-58 Thermal (SIMULATED)"
COKE = "4800001234567"
WATER = "4800002234564"
UNKNOWN = "000000"

PASS: list[str] = []
FAIL: list[str] = []
CAPTURE_FAILS: list[str] = []


def check(condition: bool, label: str) -> None:
    (PASS if condition else FAIL).append(label)
    print(("  PASS  " if condition else "  FAIL  ") + label)


class FakeWin32Print:
    """Records RAW jobs instead of talking to a real USB printer."""

    PRINTER_ENUM_LOCAL = 2
    PRINTER_ENUM_CONNECTIONS = 4

    def __init__(self) -> None:
        self.names = [PRINTER_NAME, "Microsoft Print to PDF"]
        self.status = 0
        self.attributes = 0
        self.written: list[bytes] = []
        self.docs: list[tuple] = []

    def EnumPrinters(self, flags):
        return [(None, None, name, None) for name in self.names]

    def GetDefaultPrinter(self):
        return PRINTER_NAME

    def OpenPrinter(self, name):
        return object()

    def ClosePrinter(self, handle):
        return None

    def GetPrinter(self, handle, level):
        return {"Status": self.status, "Attributes": self.attributes}

    def StartDocPrinter(self, handle, level, info):
        self.docs.append(info)
        return len(self.docs)

    def StartPagePrinter(self, handle):
        return None

    def WritePrinter(self, handle, payload):
        self.written.append(payload)
        return len(payload)

    def EndPagePrinter(self, handle):
        return None

    def EndDocPrinter(self, handle):
        return None

    def AbortPrinter(self, handle):
        return None


FAKE = FakeWin32Print()
sys.modules["win32print"] = FAKE  # type: ignore[assignment]

# ---- redirect every write away from the delivered project --------------
database.DB_PATH = DB
receipt.RECEIPTS_DIR = RECEIPTS
receipt.PRINTER_CONFIG_PATH = CONFIG
CONFIG.parent.mkdir(parents=True, exist_ok=True)
CONFIG.write_text(
    json.dumps(
        {
            "mode": "escpos",
            "printer_name": PRINTER_NAME,
            "auto_print": True,
            "cut": True,
            "feed_lines": 3,
            "bold": True,
        },
        indent=2,
    ),
    encoding="utf-8",
)

database.initialize_database(DB)
products.ensure_schema(DB)
products.bootstrap_catalog(DB)

apply_appearance()
root = ctk.CTk()
root.geometry("1160x720")
root.title("Neighborhood Mart - Stage 14 hardware smoke (SIMULATED)")
root.update()


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
    """Capture the real Tk window WITHOUT stealing keyboard focus.

    front()/SetForegroundWindow() changes the Windows foreground, which makes
    Tk drop generated key events for the next scanner simulation. PrintWindow
    renders even an occluded window, so screenshots no longer disturb the
    simulated HID input. A screen grab is used only as a fallback if the
    PrintWindow bitmap comes back blank.
    """
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


def focus_inner(widget):
    """Give the REAL tkinter.Entry keyboard focus before generating keys.

    Tk silently DROPS event_generate() key events when no widget owns focus.
    Take focus, let the queued FocusIn run (CTkEntry needs it to clear any
    placeholder on other fields), then re-assert focus. event_generate itself
    delivers synchronously, so no further pumping happens between keystrokes.
    """
    entry = getattr(widget, "_entry", widget)  # CTkEntry -> tkinter.Entry
    try:
        root.focus_force()
    except Exception:
        pass
    entry.focus_set()
    root.update()
    entry.focus_set()
    return entry


def key_scan(widget, code: str, *, slow: bool = False) -> None:
    """SIMULATED USB HID scan: fast digits + Enter into `widget`.

    <KeyPress-N> generates a real Tk KeyPress event (char='N'), which both
    inserts the character into the focused Entry and reaches the toplevel
    <Key> binding PosView installed. This is the same event stream a
    keyboard-wedge scanner produces.
    """
    entry = focus_inner(widget)
    for char in code:
        entry.focus_set()
        entry.event_generate(f"<KeyPress-{char}>")
        if slow:
            time.sleep(0.12)  # slower than SCAN_KEY_GAP_MS: human typing
    entry.focus_set()
    entry.event_generate("<Return>")
    root.update()
    pump(90)


def manual_entry(widget, code: str) -> None:
    """Manual barcode entry must keep working (no burst involved)."""
    entry = focus_inner(widget)
    widget.delete(0, "end")
    widget.insert(0, code)
    entry.focus_set()
    entry.event_generate("<Return>")
    root.update()
    pump(90)


try:
    # ==================================================================
    # 1 - Printer setup dialog (SIMULATED EnumPrinters)
    # ==================================================================
    CASHIER = {"user_id": 2, "username": "cashier", "role": "cashier"}
    shell = AppShell(
        root, user=CASHIER,
        on_logout=lambda: None,
        on_change_password=lambda: None,
        on_exit=lambda: None,
    )
    shell.pack(fill="both", expand=True)
    root.update()
    pos = shell.current_page
    pos.receipt_dir = RECEIPTS

    check(pos.__class__.__name__ == "PosView",
          "cashier lands directly on the barcode Point of Sale")
    check(pos.winfo_toplevel() is root,
          "POS widget keeps a valid Tk root reference (no shadowed _root)")
    check(getattr(pos.scan_entry, "_placeholder_text", None) is None,
          "scan field has no placeholder that can swallow a fast first scan")
    settings = receipt.printer_settings()
    check(PRINTER_NAME in settings["printers"],
          "Printer setup discovers the installed (simulated) USB printer")
    check(settings["config"]["mode"] == "escpos"
          and settings["config"]["printer_name"] == PRINTER_NAME,
          "printer selection is read from config, not hardcoded")
    check(receipt.check_printer(PRINTER_NAME)["available"],
          "selected printer reports ready")

    pos._open_printer_dialog()
    pump(220)
    dialog = pos.dialog
    check(dialog is not None and dialog.winfo_exists(),
          "Printer setup dialog opens from the POS page")
    check(any(PRINTER_NAME in label for label in dialog._labels()),
          "dialog lists the installed printers as selectable options")
    check(dialog._current_label().endswith(PRINTER_NAME),
          "dialog preselects the currently configured printer")
    shot("stage14_01_printer_setup.png", target=dialog)
    dialog.close()
    pump(150)

    # ==================================================================
    # 2 - Simulated USB scanner: single scan and repeated scan
    # ==================================================================
    key_scan(pos.scan_entry, COKE)
    check(len(pos.cart) == 1,
          "one simulated keyboard scan adds one basket line")
    check(pos.total_amount == 35.0,
          "scanned Coca-Cola is priced at PHP 35.00")
    check(pos.scan_entry.get() == "",
          "scan field is cleared after every completed scan")

    key_scan(pos.scan_entry, COKE)
    check(len(pos.cart) == 1 and pos.cart[0]["quantity"] == 2,
          "repeated scan of the same barcode increases the quantity")
    check(pos.total_amount == 70.0, "two scans total PHP 70.00")

    key_scan(pos.scan_entry, WATER)
    check(len(pos.cart) == 2, "a second product creates a second basket line")
    check(pos.total_amount == 85.0, "basket total is PHP 85.00")
    shot("stage14_02_scanner_basket.png")

    # ---- Unknown barcode -------------------------------------------
    before = len(pos.cart)
    key_scan(pos.scan_entry, UNKNOWN)
    check("No sellable product found" in str(pos.status_label.cget("text")),
          "unknown barcode shows a clear error message")
    check(len(pos.cart) == before, "unknown barcode adds nothing")
    check(pos.scan_entry.get() == "",
          "rejected barcode is cleared so the next scan cannot concatenate")

    # ---- Incomplete / accidental input ------------------------------
    key_scan(pos.scan_entry, "480")
    check("Incomplete barcode" in str(pos.status_label.cget("text")),
          "partial scan is rejected before any product lookup")
    check(len(pos.cart) == before, "partial scan adds nothing")

    # ---- Slow human typing must NOT be treated as a scan ------------
    pos.tendered_entry.delete(0, "end")
    key_scan(pos.tendered_entry, "123456", slow=True)
    check(len(pos.cart) == before,
          "slow human typing cannot trigger a product lookup")
    pos.tendered_entry.delete(0, "end")

    # ---- Scan while focus is on a DIFFERENT field --------------------
    # This is the key hardware requirement: no repeated clicking.
    pos.tendered_entry.delete(0, "end")
    pos.tendered_entry.insert(0, "100")
    key_scan(pos.tendered_entry, COKE)
    check(any(item["barcode"] == COKE and item["quantity"] == 3
              for item in pos.cart),
          "a scan focused on the tendered field is redirected and added")
    check(pos.tendered_entry.get() == "100",
          "stray scanner digits are removed from the other field")
    check(pos.scan_entry.get() == "",
          "redirected scan leaves the barcode field clean")
    pos.tendered_entry.delete(0, "end")
    pump(220)

    # ---- Manual entry still works -----------------------------------
    manual_entry(pos.scan_entry, WATER)
    check(any(item["barcode"] == WATER and item["quantity"] == 2
              for item in pos.cart),
          "manual barcode entry still adds to the basket")

    # ==================================================================
    # 3 - Successful transaction -> archive -> ESC/POS print
    # ==================================================================
    coke_before = stock(COKE)
    water_before = stock(WATER)
    jobs_before = len(FAKE.written)
    pos._use_exact_total()
    pos._confirm()
    pump(400)

    check(scalar("SELECT COUNT(*) FROM store_sales") == 1,
          "exactly one sale was committed")
    check(scalar("SELECT COUNT(*) FROM store_sale_items") == 2,
          "two immutable product snapshots were saved")
    check(scalar("SELECT COUNT(*) FROM stock_out_details") == 2,
          "two stock-out lines were written inside the same transaction")
    check(stock(COKE) == coke_before - 3,
          "Coca-Cola inventory was deducted by 3")
    check(stock(WATER) == water_before - 2,
          "water inventory was deducted by 2")
    check(scalar("SELECT currency_code FROM store_sales") == "PHP",
          "sale records PHP as its currency")

    archived = list(RECEIPTS.glob("SM-*.txt"))
    check(len(archived) == 1, "exactly one receipt was archived")
    text = archived[0].read_text(encoding="utf-8")
    for field in ("Txn no.", "Date/time", "Subtotal", "TOTAL",
                  "Tendered", "Change", format_money(0), COKE):
        check(field in text, f"archived receipt contains {field!r}")

    check(len(FAKE.written) == jobs_before + 1,
          "exactly one RAW ESC/POS job was sent after the sale")
    job = FAKE.written[-1]
    check(job.startswith(b"\x1b@\x1bt\x00"),
          "ESC/POS job begins with initialize + PC437")
    check(job.endswith(b"\x1dVB\x00"), "ESC/POS job ends with a partial cut")
    check(b"\r\n" in job and b"\x1ba\x01" in job,
          "ESC/POS job uses CRLF lines and centre alignment")
    check(all(byte < 128 for byte in job),
          "ESC/POS payload is single-width ASCII (peso sign transliterated)")
    check(b"SM-" in job and b"TOTAL" in job and b"P" in job,
          "ESC/POS job carries the transaction number and peso amounts")
    check("confirmed" in str(pos.status_label.cget("text")),
          "cashier sees a confirmation message")
    shot("stage14_03_sale_and_print.png")

    # ==================================================================
    # 4 - Printer disconnection: sale must survive, no duplicate receipt
    # ==================================================================
    FAKE.names = ["Microsoft Print to PDF"]  # USB printer unplugged
    check(not receipt.check_printer(PRINTER_NAME)["available"],
          "disconnected printer is detected before a job is opened")
    pos.scan_entry.delete(0, "end")
    manual_entry(pos.scan_entry, COKE)
    pos._use_exact_total()
    jobs_before = len(FAKE.written)
    pos._confirm()
    pump(400)
    check(scalar("SELECT COUNT(*) FROM store_sales") == 2,
          "the second sale is committed even though the printer is gone")
    check(len(FAKE.written) == jobs_before,
          "no second, wrong receipt was printed on the default printer")
    check(len(list(RECEIPTS.glob("SM-*.txt"))) == 2,
          "the failed-print receipt is still archived")
    check("ESC/POS print failed" in str(pos.status_label.cget("text")),
          "cashier is told printing failed and the receipt was archived")
    shot("stage14_04_printer_offline.png")

    # ==================================================================
    # 5 - Failed transactions must write nothing
    # ==================================================================
    sales_before = scalar("SELECT COUNT(*) FROM store_sales")
    pos.cart = []
    manual_entry(pos.scan_entry, COKE)
    pos.tendered_entry.delete(0, "end")
    pos.tendered_entry.insert(0, "1")  # less than the total
    pos._confirm()
    pump(200)
    check(scalar("SELECT COUNT(*) FROM store_sales") == sales_before,
          "insufficient tendered amount writes no sale")
    check(len(FAKE.written) == jobs_before,
          "a rejected sale never starts a print job")
    pos.cart = []
    pos._render_cart()

    # ==================================================================
    # 6 - Reprint after the printer is reconnected
    # ==================================================================
    FAKE.names = [PRINTER_NAME, "Microsoft Print to PDF"]
    sale_id = scalar("SELECT sale_id FROM store_sales ORDER BY sale_id DESC")
    pos._reprint(sale_id)
    pump(250)
    check(len(FAKE.written) == jobs_before + 1,
          "Reprint sends one new RAW job after the printer is back")
    check(any("REPRINT" in path.name for path in RECEIPTS.glob("*.txt")),
          "a separate reprint copy is archived")
    check("reprinted" in str(pos.status_label.cget("text")),
          "cashier sees the reprint result")

except Exception:
    FAIL.append("unexpected exception during the smoke run")
    traceback.print_exc()
finally:
    try:
        root.destroy()
    except Exception:
        pass
    permissions.clear_session()

REAL_HASH_AFTER = hashlib.sha256(REAL_DB.read_bytes()).hexdigest()
check(REAL_HASH_BEFORE == REAL_HASH_AFTER,
      "delivered database byte-for-byte unchanged")
check(not (ROOT / "config").exists(),
      "no printer config was written into the delivered project")
check(not CAPTURE_FAILS,
      "all screenshots rendered non-blank"
      + (f" (bad: {', '.join(CAPTURE_FAILS)})" if CAPTURE_FAILS else ""))

print("\n--- Stage 14 smoke summary (SIMULATED hardware only) ---")
print(f"PASS {len(PASS)}   FAIL {len(FAIL)}")
for item in FAIL:
    print("FAILED:", item)
print("simulated ESC/POS jobs:", len(FAKE.written))
print("receipts archived:", len(list(RECEIPTS.glob("*.txt"))))
print("screenshots:", len(list(OUT.glob("stage14_*.png"))))
print("REAL HARDWARE TESTS: NOT PERFORMED - see the hand-off report.")
TEMP.cleanup()
sys.exit(1 if FAIL else 0)
