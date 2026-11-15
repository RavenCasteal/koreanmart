"""
Stage 18 GUI smoke test - batch / supplier / backup tooling.

Opens the REAL CustomTkinter windows and drives the real pages:

  manager Inventory -> "Database tools" dialog -> add a supplier -> run
  reconciliation -> create a verified backup -> restore is gated behind a
  confirmation tick box -> close
  Inventory -> "Stock in" -> supplier picker + Cost/unit field -> save ->
  the delivery creates a batch with the cost / supplier snapshot
  data layer -> FEFO stock-out consumes the earliest-expiring batch

ALL data goes to a temporary SQLite file and temporary receipt / report /
backup folders. database.DB_PATH, receipt.RECEIPTS_DIR, sales_report.REPORTS_DIR
and backups.BACKUP_DIR are redirected BEFORE any widget is built. No printer,
scanner or real database is written to. The delivered database is only hashed.

Screenshots go to assets/images/stage18_*.png.
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

import backups
import batches
import database
import permissions
import products
import receipt
import sales_report
import stock_repository as stock
from components.shell import AppShell
from components.theme import apply_appearance
from pages.database_tools import DatabaseToolsDialog

OUT = ROOT / "assets" / "images"
OUT.mkdir(parents=True, exist_ok=True)
REAL_DB = ROOT / "database" / "ramen_inventory.db"

TEMP = tempfile.TemporaryDirectory()
TMP = Path(TEMP.name)
DB = TMP / "ramen_inventory.db"
BACKUPS = TMP / "backups"

REAL_HASH_BEFORE = hashlib.sha256(REAL_DB.read_bytes()).hexdigest()

PASS: list[str] = []
FAIL: list[str] = []
CAPTURE_FAILS: list[str] = []

# ---- redirect every write away from the delivered project --------------
database.DB_PATH = DB
receipt.RECEIPTS_DIR = TMP / "receipts"
sales_report.REPORTS_DIR = TMP / "reports"
backups.BACKUP_DIR = BACKUPS

# ---- exactly what main.py does on first launch --------------------------
database.initialize_database(DB)
products.ensure_schema(DB)
products.bootstrap_catalog(DB)

MANAGER = database.verify_credentials(
    database.DEFAULT_ADMIN_USERNAME, database.DEFAULT_ADMIN_PASSWORD, DB
)
assert MANAGER is not None

apply_appearance()
root = ctk.CTk()
root.title("Neighborhood Mart - Stage 18 database tools smoke")
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


def front(win) -> None:
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
    light = sum(
        1 for r, g, b in small.getdata()
        if r > 225 and g > 218 and b > 205
    )
    return light / (80 * 50) > 0.25


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
        ("biImportantColors", wintypes.DWORD),
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
    win = target or root
    win.update_idletasks()
    win.update()
    image = grab_window(win)
    if not looks_like_app(image):
        front(win)
        time.sleep(0.28)
        win.update()
        x, y = win.winfo_rootx(), win.winfo_rooty()
        w, h = win.winfo_width(), win.winfo_height()
        image = ImageGrab.grab(bbox=(x, y, x + w, y + h))
    if not looks_like_app(image):
        CAPTURE_FAILS.append(name)
    image.save(OUT / name)
    print(f"  SHOT  {name}  {image.size}")


def widgets(node):
    yield node
    for child in node.winfo_children():
        yield from widgets(child)


def cget_text(widget) -> str:
    try:
        return str(widget.cget("text"))
    except Exception:
        return ""


def scalar(sql: str, params: tuple = ()) -> object:
    conn = sqlite3.connect(DB)
    try:
        row = conn.execute(sql, params).fetchone()
        return None if row is None else row[0]
    finally:
        conn.close()


try:
    # ==================================================================
    # 1 - Launch-time migration
    # ==================================================================
    check(int(scalar("PRAGMA user_version")) == 4,
          "delivered schema version is still v4")
    check(str(scalar("PRAGMA integrity_check")) == "ok",
          "temporary database integrity is ok")
    check(int(scalar("SELECT COUNT(*) FROM stock_batches")) == 18,
          "opening batches were created for all 18 seeded products")
    check(int(scalar(
        "SELECT COUNT(*) FROM stock_batches WHERE unit_cost IS NOT NULL"
    )) == 0, "no historical purchase cost was invented")
    check(batches.reconcile(db_path=DB, actor=MANAGER)["ok"],
          "batch totals reconcile with product on-hand balances")

    shell = AppShell(
        root, user=MANAGER,
        on_logout=lambda: None,
        on_change_password=lambda: None,
        on_exit=lambda: None,
    )
    shell.pack(fill="both", expand=True)
    root.update()
    shell.navigate("inventory")
    pump(400)
    page = shell.current_page
    check(page.__class__.__name__ == "InventoryView",
          "manager can open the Inventory screen")
    check(any("Database tools" in cget_text(w) for w in widgets(page)),
          "toolbar offers the manager-only Database tools button")
    shot("stage18_01_inventory_toolbar.png")

    # ==================================================================
    # 2 - Database tools dialog
    # ==================================================================
    page._open_database_tools()
    pump(350)
    dialog = page.dialog
    check(isinstance(dialog, DatabaseToolsDialog),
          "Database tools dialog opens from the Inventory screen")
    check(hasattr(dialog, "supplier_name")
          and hasattr(dialog, "confirm_restore")
          and hasattr(dialog, "batch_body"),
          "dialog exposes suppliers, batches and backup controls")
    shot("stage18_02_database_tools.png", dialog)

    dialog.supplier_name.insert(0, "Metro Wholesale")
    dialog.supplier_contact.insert(0, "02-8123-4567")
    dialog._add_supplier()
    pump(150)
    check("Metro Wholesale" in str(dialog.supplier_list.cget("text")),
          "new supplier appears in the dialog")
    check(int(scalar("SELECT COUNT(*) FROM suppliers")) == 1,
          "supplier was persisted to SQLite")

    dialog._reconcile()
    check("OK" in str(dialog.status.cget("text")),
          "reconciliation reports a clean ledger")
    shot("stage18_03_supplier_added_reconciled.png", dialog)

    dialog._restore_backup()
    check("confirmation" in str(dialog.status.cget("text")).lower(),
          "restore is refused without the confirmation tick box")

    dialog._create_backup()
    pump(150)
    backup_files = list(BACKUPS.glob("*.db"))
    check(len(backup_files) == 1,
          "create backup writes exactly one verified .db file")
    check("Backup verified" in str(dialog.status.cget("text")),
          "backup result is shown in the dialog")
    check(BACKUPS.is_dir() and not (ROOT / "backups").exists(),
          "no backup folder was created in the delivered project")
    shot("stage18_04_backup_created.png", dialog)
    dialog.close()
    pump(150)

    # ==================================================================
    # 3 - Stock-in form: supplier + unit cost
    # ==================================================================
    page._open_stock_in()
    pump(350)
    form = page.dialog
    check(hasattr(form, "supplier_combo"),
          "stock-in form has an optional supplier picker")
    row = form.line_rows[0]
    check(row.get("cost") is not None,
          "stock-in form has a Cost/unit field on every delivery line")
    check(row.get("expiry") is not None,
          "stock-in form still has the expiry field")
    shot("stage18_05_stock_in_form.png", form)

    row["quantity"].insert(0, "10")
    row["cost"].insert(0, "12.50")
    row["expiry"].insert(0, "2027-03-01")
    supplier_label = next(
        label for label in form._supplier_ids
        if label != "- No supplier -"
    )
    form.supplier_combo.set(supplier_label)
    form.reference_entry.insert(0, "PO-1042")
    form._save()
    pump(300)

    check(int(scalar("SELECT COUNT(*) FROM stock_in_transactions "
                     "WHERE reference_no = 'PO-1042'")) == 1,
          "stock-in header with the original reference number was saved")
    batch_row = None
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        batch_row = dict(conn.execute(
            "SELECT b.*, s.name AS supplier_name "
            "FROM stock_batches AS b "
            "LEFT JOIN suppliers AS s ON s.supplier_id = b.supplier_id "
            "WHERE b.expiration_date = '2027-03-01'"
        ).fetchone())
    finally:
        conn.close()
    check(batch_row is not None, "delivery created one received batch")
    if batch_row:
        check(float(batch_row["unit_cost"]) == 12.5,
              "unit cost is snapshotted on the received batch")
        check(batch_row["supplier_name"] == "Metro Wholesale",
              "supplier is snapshotted on the received batch")
        check(float(batch_row["received_qty"]) == 10.0,
              "received quantity matches the delivery line")
    check(batches.reconcile(db_path=DB, actor=MANAGER)["ok"],
          "ledger still reconciles after the delivery")
    shot("stage18_06_after_stock_in.png")

    # ==================================================================
    # 4 - FEFO stock-out on the received batch
    # ==================================================================
    if batch_row is None:
        raise RuntimeError("no delivery batch to test FEFO against")
    pid = int(batch_row["ingredient_id"])
    conn = database.get_connection(DB)
    try:
        fefo_order = [int(r["batch_id"]) for r in batches.fefo_batches(conn, pid)]
    finally:
        conn.close()
    result = stock.record_stock_out(
        [{"ingredient_id": pid, "quantity": 3, "notes": "smoke"}],
        "Damaged", notes="stage18 smoke", user_id=1,
        db_path=DB, actor={"user_id": 1, "role": "manager"},
    )
    check(int(result["allocation_count"]) >= 1,
          "stock-out recorded FEFO batch allocations")
    conn = sqlite3.connect(DB)
    try:
        allocations = conn.execute(
            "SELECT a.batch_id, a.quantity FROM stock_batch_allocations AS a "
            "JOIN stock_out_details AS d "
            "  ON d.stock_out_detail_id = a.stock_out_detail_id "
            "WHERE d.stock_out_id = ?", (int(result["transaction_id"]),),
        ).fetchall()
    finally:
        conn.close()
    check(bool(allocations) and int(allocations[0][0]) == fefo_order[0],
          "stock-out starts with the first FEFO batch")
    check(batches.reconcile(db_path=DB, actor=MANAGER)["ok"],
          "ledger still reconciles after the FEFO stock-out")

    # ==================================================================
    # 5 - Delivery hygiene
    # ==================================================================
    check(REAL_HASH_BEFORE == hashlib.sha256(REAL_DB.read_bytes()).hexdigest(),
          "delivered database is byte-for-byte unchanged")
    check(not (ROOT / "backups").exists(),
          "no backups folder was created in the delivered project")
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
    print(f"screenshots: {len(list(OUT.glob('stage18_*.png')))}")
    if CAPTURE_FAILS:
        print(f"capture fallbacks: {CAPTURE_FAILS}")
    print("=" * 62)
    try:
        root.destroy()
    except Exception:
        pass
    TEMP.cleanup()
    sys.exit(1 if FAIL else 0)
