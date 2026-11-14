"""
Stage 13 GUI smoke test (temporary verification script).

Opens the REAL CustomTkinter window and drives the real Reports page:

  manager : 12-item sidebar -> Sales Reports -> KPI cards, payment mix,
            cashier totals, top products, category mix, 7-day trend and the
            transaction list (including a voided sale) -> Previous / Today
            -> Print / save Z-read -> Export CSV
  cashier : only one sidebar item, navigate("reports") is refused, and a
            directly constructed ReportsView shows the denied state

ALL data goes to a temporary SQLite file and a temporary reports folder.
database.DB_PATH and sales_report.REPORTS_DIR are redirected before ANY
widget is built, so the delivered database is never touched. Printing is
patched out; Z-reads and CSV files are still archived in the temp folder.
Screenshots go to assets/images/stage13_*.png.
"""

from __future__ import annotations

import ctypes
import hashlib
import sys
import tempfile
import time
from ctypes import wintypes
from datetime import date
from pathlib import Path

import customtkinter as ctk
from PIL import Image, ImageGrab

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import database
import permissions
import products
import receipt
import sales_report
import store_sales
import users_repository
from components.shell import AppShell
from components.theme import apply_appearance
from formatting import format_money
from pages.reports import ReportsView

OUT = ROOT / "assets" / "images"
OUT.mkdir(parents=True, exist_ok=True)
REAL_DB = ROOT / "database" / "ramen_inventory.db"

TEMP = tempfile.TemporaryDirectory()
TMP = Path(TEMP.name)
DB = TMP / "ramen_inventory.db"
REPORTS = TMP / "reports"

REAL_HASH_BEFORE = hashlib.sha256(REAL_DB.read_bytes()).hexdigest()

COKE = "4800001234567"
WATER = "4800002234564"
CLOUD9 = "4800005234565"

# ---- redirect every write away from the delivered database --------------
database.DB_PATH = DB
sales_report.REPORTS_DIR = REPORTS
receipt.print_receipt = lambda text, path=None, printer_name=None: {
    "method": "smoke_test",
    "detail": "Smoke test: Z-read archived, printer not used.",
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
CASHIER_ID = users_repository.create_user(
    "counter", "Counter2026!", "Counter Staff", "cashier",
    db_path=DB, actor=MANAGER,
)
CASHIER = database.verify_credentials("counter", "Counter2026!", DB)
assert CASHIER is not None

# ---- seed a realistic day with three sales, one of them voided ----------
def sale(cart, method, user_id):
    return store_sales.confirm_sale(
        cart, method, "5000",
        user_id=user_id, db_path=DB, actor=MANAGER,
    )


first = sale([(COKE, 2), (CLOUD9, 1)], "Cash", MANAGER["user_id"])   # 82.00
second = sale([(WATER, 3)], "Card", CASHIER_ID)                     # 45.00
third = sale([(CLOUD9, 2)], "E-wallet", CASHIER_ID)                  # 24.00
store_sales.void_sale(
    third["sale_id"], "Customer changed mind",
    user_id=MANAGER["user_id"], db_path=DB, actor=MANAGER,
)
EXPECTED_GROSS = 151.00
EXPECTED_NET = 127.00
TODAY = date.today().isoformat()

# ---- window -------------------------------------------------------------
apply_appearance()
root = ctk.CTk()
root.title("Stage 13 GUI smoke test")
root.geometry("1160x720+40+40")
root.configure(fg_color="#FAF6EF")

PASS: list[str] = []
FAIL: list[str] = []
CAPTURE_FAILS: list[str] = []


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
        gdi.GetDIBits(
            mem_dc, bitmap, 0, height, buffer, ctypes.byref(header), 0
        )
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
    image = None
    for _attempt in range(4):
        front(win, others=[root] if win is not root else ())
        time.sleep(0.28)
        win.update()
        x, y = win.winfo_rootx(), win.winfo_rooty()
        w, h = win.winfo_width(), win.winfo_height()
        image = ImageGrab.grab(bbox=(x, y, x + w, y + h))
        if looks_like_app(image):
            break
    assert image is not None
    if not looks_like_app(image):
        image = grab_window(win)
    if not looks_like_app(image):
        CAPTURE_FAILS.append(name)
    image.save(OUT / name)
    print(f"  SHOT  {name}  {image.size}")


def all_text(widget) -> str:
    """Collect every label / button caption in a widget tree."""
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


def build_shell(user):
    shell = AppShell(
        root,
        user=user,
        on_logout=lambda: None,
        on_change_password=lambda: None,
        on_exit=lambda: None,
    )
    shell.pack(fill="both", expand=True)
    root.update()
    return shell


try:
    # ==================================================================
    # MANAGER - the new twelfth page
    # ==================================================================
    shell = build_shell(MANAGER)
    check(len(shell.sidebar._buttons) == 12,
          "manager sidebar shows all 12 navigation items")
    check("reports" in shell.allowed_keys,
          "Sales Reports is registered for the manager")
    check("Sales Reports" in all_text(shell.sidebar),
          "sidebar contains the Sales Reports button")

    shell.navigate("reports")
    pump(320)
    page = shell.current_page
    check(shell.active_key == "reports", "navigation lands on Sales Reports")
    check(isinstance(page, ReportsView),
          "ReportsView (not a placeholder) is the live page")
    check(page.summary is not None, "the report loaded a daily summary")
    check(page.summary["date"] == TODAY, "report defaults to today's date")
    check(abs(page.summary["gross_sales"] - EXPECTED_GROSS) < 0.01,
          "gross sales include the voided sale")
    check(abs(page.summary["net_sales"] - EXPECTED_NET) < 0.01,
          "net sales exclude the voided sale")
    check(page.summary["voided_count"] == 1,
          "one voided sale is reported")
    check(page.summary["items_sold"] == 6,
          "items sold counts only completed baskets")

    text = all_text(page)
    for caption in (
        "Sales reports", "Gross sales", "Voids", "Net sales",
        "Transactions", "Average basket", "Payment mix", "Cashier totals",
        "Top products", "Category mix", "Last 7 days", "Transactions",
        "Print / save Z-read", "Export CSV",
    ):
        check(caption in text, f"screen shows '{caption}'")
    check(format_money(EXPECTED_NET) in text,
          "net sales are rendered with the peso formatter")
    check("PHP" in text, "screen states the PHP currency")
    check(first["transaction_number"] in text,
          "completed transaction appears in the list")
    check(third["transaction_number"] in text,
          "voided transaction is kept in the list")
    check("Voided" in text, "voided status is visible")
    check("Coca-Cola Original 500 ml" in text,
          "top products use the immutable sale snapshot")
    check(len(page.summary["by_payment"]) == 2,
          "payment mix contains Cash and Card only")
    check("counter" in text or CASHIER["username"] in text,
          "cashier totals show the counter account")
    shot("stage13_01_manager_reports.png")

    # ---- date navigation ------------------------------------------------
    page._change_day(-1)
    pump(200)
    check(page.date_entry.get() != TODAY,
          "Previous moves to the day before")
    check(page.summary["date"] != TODAY,
          "the report re-reads the previous day")
    check(page.summary["transaction_count"] == 0,
          "the previous day has no sales (zero-filled report)")
    shot("stage13_02_reports_empty_day.png")
    page._today()
    pump(200)
    check(page.date_entry.get() == TODAY, "Today returns to today's date")
    check(page.summary["transaction_count"] == 3,
          "today shows all three sales again")

    # ---- Z-read and CSV -------------------------------------------------
    page.auto_print = False
    page.report_dir = REPORTS
    page._print_z()
    pump(160)
    z_path = REPORTS / f"Z-READ-{TODAY}.txt"
    check(z_path.exists(), "Z-read was archived in reports/")
    if z_path.exists():
        z_text = z_path.read_text(encoding="utf-8")
        check("NET SALES" in z_text, "Z-read contains the net sales section")
        check(format_money(EXPECTED_NET) in z_text,
              "Z-read prints the peso net sales total")
        check("Customer changed mind" in z_text,
              "Z-read lists the void reason")
        check("\u00a5" not in z_text and "\uffe5" not in z_text,
              "Z-read never uses a yen / yuan sign")
    check("Z-read saved" in page.status_label.cget("text"),
          "screen confirms the Z-read was saved")

    page._export_csv()
    pump(160)
    csv_path = REPORTS / f"sales-{TODAY}.csv"
    check(csv_path.exists(), "CSV export was written to reports/")
    if csv_path.exists():
        lines = csv_path.read_text(encoding="utf-8-sig").splitlines()
        check(len(lines) == 4, "CSV has a header and three sale rows")
        check("Transaction no." in lines[0], "CSV has a header row")
        check("voided" in "\n".join(lines), "CSV keeps the voided sale")
    check("exported" in page.status_label.cget("text"),
          "screen confirms the CSV export")

    # ==================================================================
    # CASHIER - no page, no totals
    # ==================================================================
    shell.destroy()
    root.update()
    cashier_shell = build_shell(CASHIER)
    check(len(cashier_shell.sidebar._buttons) == 1,
          "cashier sidebar still shows ONLY Point of Sale")
    check("reports" not in cashier_shell.allowed_keys,
          "reports is not an allowed page for a cashier")
    cashier_shell.navigate("reports")
    pump(160)
    check(cashier_shell.active_key == "pos",
          "navigate('reports') is refused for a cashier")
    check(not permissions.can(CASHIER, permissions.CAP_VIEW_REPORTS),
          "cashier lacks the view_reports capability")
    shot("stage13_03_cashier_pos_only.png")

    denied = ReportsView(root, user=CASHIER)
    denied.pack(fill="both", expand=True)
    pump(160)
    check("Manager access required" in all_text(denied),
          "a forced ReportsView shows the denied state")
    check(denied.summary is None, "no sales data was loaded for a cashier")
    shot("stage13_04_reports_denied.png")
    denied.destroy()
    cashier_shell.destroy()
    permissions.clear_session()

    # ==================================================================
    # The delivered database must be byte-for-byte untouched
    # ==================================================================
    check(hashlib.sha256(REAL_DB.read_bytes()).hexdigest() == REAL_HASH_BEFORE,
          "delivered database/ramen_inventory.db was not modified")
    check(not (ROOT / "reports").exists(),
          "smoke wrote no reports into the delivered project folder")

finally:
    print("\n--- Stage 13 smoke summary ---")
    print(f"PASS: {len(PASS)}   FAIL: {len(FAIL)}")
    print("capture fallbacks:", CAPTURE_FAILS)
    print("screenshots:", len(list(OUT.glob("stage13_*.png"))))
    for label in FAIL:
        print("  FAILED:", label)
    TEMP.cleanup()
    root.destroy()
    sys.exit(1 if FAIL else 0)
