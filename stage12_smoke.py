"""
Stage 12 GUI smoke test (temporary verification script).

Opens the REAL CustomTkinter windows and drives the real dialogs:

  manager : Products page -> add a barcoded product -> edit its price ->
            search -> deactivate -> see the store dashboard
  cashier : sees ONLY the barcode Point of Sale -> scans two products with
            the real <Return> binding -> builds a basket -> confirms a sale
            -> views / reprints the receipt -> cannot void
  manager : voids the sale through the real VoidDialog and gets the stock back

ALL data goes to a temporary SQLite file: database.DB_PATH is redirected
before any widget is built, so the delivered database is never touched.
Receipts are archived in the temp folder and printing is skipped.
Screenshots go to assets/images/stage12_*.png.
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

import database
import pages.products as products_page
import pages.store_pos as pos_page
import permissions
import products
import receipt
import store_sales
import users_repository as users
from components.shell import AppShell
from components.theme import apply_appearance
from formatting import CURRENCY_SYMBOL, format_money

OUT = ROOT / "assets" / "images"
OUT.mkdir(parents=True, exist_ok=True)
REAL_DB = ROOT / "database" / "ramen_inventory.db"
ERROR_LOG = ROOT / "logs" / "error.log"

TEMP = tempfile.TemporaryDirectory()
TMP = Path(TEMP.name)
DB = TMP / "ramen_inventory.db"
RECEIPTS = TMP / "receipts"

REAL_HASH_BEFORE = hashlib.sha256(REAL_DB.read_bytes()).hexdigest()
LOG_SIZE_BEFORE = ERROR_LOG.stat().st_size if ERROR_LOG.exists() else -1

COKE = "4800001234567"
WATER = "4800002234564"
NEW_CODE = "99999999"

# ---- redirect every write away from the delivered database -------------
database.DB_PATH = DB
receipt.RECEIPTS_DIR = RECEIPTS
receipt.print_receipt = lambda text, path=None, printer_name=None: {
    "method": "smoke_test",
    "detail": "Smoke test: receipt archived, printer not used.",
    "ok": True,
}


class _ConfirmYes:
    """A messagebox would block a non-interactive smoke run."""

    @staticmethod
    def askyesno(*args, **kwargs):
        return True


products_page.messagebox = _ConfirmYes()
pos_page.messagebox = _ConfirmYes()

# ---- exactly what main.py does on the first Stage 12 launch -------------
database.initialize_database(DB)
products.ensure_schema(DB)
products.bootstrap_catalog(DB)

# Use the REAL authentication path for both sessions.
MANAGER = database.verify_credentials(
    database.DEFAULT_ADMIN_USERNAME, database.DEFAULT_ADMIN_PASSWORD, DB
)
assert MANAGER is not None
users.create_user(
    "counter", "Counter2026!", "Counter Staff", "cashier",
    db_path=DB, actor=MANAGER,
)
CASHIER = database.verify_credentials("counter", "Counter2026!", DB)
assert CASHIER is not None

# ---- track dialogs ------------------------------------------------------
DIALOGS: dict[str, object] = {}


def _tracked(name, base):
    class Tracked(base):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            DIALOGS[name] = self
    return Tracked


products_page.ProductDialog = _tracked("product", products_page.ProductDialog)
pos_page.ReceiptDialog = _tracked("receipt", pos_page.ReceiptDialog)
pos_page.VoidDialog = _tracked("void", pos_page.VoidDialog)

# ---- window -------------------------------------------------------------
apply_appearance()
root = ctk.CTk()
root.title("Stage 12 GUI smoke test")
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
    hwnd = user.GetAncestor(win.winfo_id(), 2)  # GA_ROOT
    rect = wintypes.RECT()
    user.GetWindowRect(hwnd, ctypes.byref(rect))
    width, height = rect.right - rect.left, rect.bottom - rect.top
    hdc = user.GetWindowDC(hwnd)
    mem_dc = gdi.CreateCompatibleDC(hdc)
    bitmap = gdi.CreateCompatibleBitmap(hdc, width, height)
    old = gdi.SelectObject(mem_dc, bitmap)
    try:
        user.PrintWindow(hwnd, mem_dc, 2)  # PW_RENDERFULLCONTENT
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


def dialog(key: str):
    value = DIALOGS.pop(key, None)
    if value is None:
        raise RuntimeError(f"dialog {key} was not created")
    pump()
    return value


def all_text(widget) -> str:
    """Collect every label/button caption in a widget tree (real UI text)."""
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


def query(sql: str, params: tuple = ()) -> list[dict]:
    with database.transaction(DB) as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def scalar(sql: str, params: tuple = ()) -> object:
    rows = query(sql, params)
    return list(rows[0].values())[0] if rows else None


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


def set_pos_offline(shell) -> object:
    """Never touch the Windows printer; still archive every receipt."""
    page = shell.current_page
    page.auto_print = False
    page.receipt_dir = RECEIPTS
    return page


try:
    # ==================================================================
    # STARTUP - the one-time convenience-store catalog reset
    # ==================================================================
    check(scalar("SELECT COUNT(*) FROM ingredients WHERE is_active = 1 "
                 "AND barcode <> ''") == 18,
          "startup seeded 18 barcoded convenience-store products")
    check(scalar("SELECT COUNT(*) FROM ingredients WHERE barcode = '' "
                 "AND is_active = 0") == 8,
          "8 ramen demo ingredients were soft-retired, not deleted")
    check(scalar("SELECT COUNT(*) FROM users") == 2,
          "both user accounts survived the catalog reset")
    check(scalar("SELECT COUNT(*) FROM stock_in_details") == 18,
          "opening balances were written as real stock-in lines")
    check(scalar("PRAGMA user_version") == database.SCHEMA_VERSION,
          "legacy v4 migration chain was left intact")

    # ==================================================================
    # MANAGER - Products / barcode catalog
    # ==================================================================
    shell = build_shell(MANAGER)
    check(len(shell.sidebar._buttons) == 12,
          "manager sidebar shows all 12 navigation items")
    check("products" in shell.allowed_keys,
          "Products is a manager-only page")
    check(shell.active_key == "dashboard", "manager lands on the Dashboard")
    check("Neighborhood Mart" in all_text(shell.current_page),
          "Dashboard is branded as the convenience store")
    shot("stage12_01_manager_dashboard.png")

    shell.navigate("products")
    pump()
    page = shell.current_page
    check(shell.active_key == "products", "manager can open Products")
    check(page.allowed is True, "Products page opens for a manager")
    check(len(page.summary_frame.winfo_children()) == 4,
          "Products page shows 4 summary cards")
    check(len(page.rows_frame.winfo_children()) == 18,
          "Products table lists all 18 seeded products")
    check("18 product(s)" in str(page.table_note.cget("text")),
          "table note reports 18 products")
    check(CURRENCY_SYMBOL in all_text(page),
          "product prices are labelled in Philippine peso")
    shot("stage12_02_manager_products.png")

    # ---- Add a product through the REAL dialog ------------------------
    page._add_product()
    add_dialog = dialog("product")
    shot("stage12_03_add_product_dialog.png", add_dialog)
    check("PHP" in all_text(add_dialog),
          "product dialog states prices are PHP")
    add_dialog.name.insert(0, "Smoke Test Soda")
    add_dialog.barcode.insert(0, NEW_CODE)
    add_dialog._choose_category("Beverages")
    add_dialog.unit.delete(0, "end")
    add_dialog.unit.insert(0, "can")
    add_dialog.price.delete(0, "end")
    add_dialog.price.insert(0, "27.50")
    add_dialog.minimum.delete(0, "end")
    add_dialog.minimum.insert(0, "3")
    add_dialog.opening.delete(0, "end")
    add_dialog.opening.insert(0, "10")
    add_dialog._save()
    pump(220)

    row = scalar("SELECT COUNT(*) FROM ingredients WHERE barcode = ?", (NEW_CODE,))
    check(row == 1, "new barcoded product was saved to SQLite")
    new_product = query("SELECT * FROM ingredients WHERE barcode = ?", (NEW_CODE,))
    if new_product:
        item = new_product[0]
        check(item["name"] == "Smoke Test Soda", "new product name is saved")
        check(item["category"] == "Beverages", "category dropdown was applied")
        check(float(item["unit_price"]) == 27.5, "price saved as PHP 27.50")
        check(float(item["quantity_on_hand"]) == 10, "opening stock is 10 cans")
        check(int(item["is_sellable"]) == 1, "new product is scannable / sellable")
    check(scalar("SELECT COUNT(*) FROM stock_in_details WHERE ingredient_id = "
                 "(SELECT ingredient_id FROM ingredients WHERE barcode = ?)",
                 (NEW_CODE,)) == 1,
          "opening stock created a real stock-in detail line")
    page.refresh()
    pump()
    check(len(page.rows_frame.winfo_children()) == 19,
          "Products table refreshes to 19 products")
    shot("stage12_04_products_after_add.png")

    # ---- Edit price without touching physical stock -------------------
    target = query("SELECT * FROM ingredients WHERE barcode = ?", (NEW_CODE,))[0]
    page._edit_product(target)
    edit_dialog = dialog("product")
    check(str(edit_dialog.opening.cget("state")) == "disabled",
          "edit mode locks opening stock (use Inventory instead)")
    edit_dialog.price.delete(0, "end")
    edit_dialog.price.insert(0, "29.00")
    edit_dialog._save()
    pump(200)
    check(scalar("SELECT unit_price FROM ingredients WHERE barcode = ?",
                 (NEW_CODE,)) == 29.0,
          "edited price is PHP 29.00")
    check(scalar("SELECT quantity_on_hand FROM ingredients WHERE barcode = ?",
                 (NEW_CODE,)) == 10,
          "editing a product never changes physical stock")

    # ---- Live search ---------------------------------------------------
    page.search_entry.delete(0, "end")
    page.search_entry.insert(0, "Smoke Test")
    page.refresh()
    pump(150)
    check(len(page.rows_frame.winfo_children()) == 1,
          "search filter narrows the table to one product")
    check("1 product(s)" in str(page.table_note.cget("text")),
          "table note reflects the filtered count")

    # ---- Deactivation is a status change, not a delete ----------------
    page._deactivate(query(
        "SELECT * FROM ingredients WHERE barcode = ?", (NEW_CODE,)
    )[0])
    pump(150)
    check(scalar("SELECT is_active FROM ingredients WHERE barcode = ?",
                 (NEW_CODE,)) == 0,
          "deactivating retires the product without deleting it")
    check(scalar("SELECT COUNT(*) FROM ingredients WHERE barcode = ?",
                 (NEW_CODE,)) == 1,
          "deactivated product row still exists with its history")
    check(products.find_by_barcode(NEW_CODE, DB) is None,
          "deactivated barcode no longer scans at the POS")
    page.search_entry.delete(0, "end")
    page.refresh()
    pump()
    check(len(page.rows_frame.winfo_children()) == 18,
          "deactivated product disappears from the catalog table")

    # ==================================================================
    # CASHIER - barcode basket POS
    # ==================================================================
    shell.destroy()
    shell = build_shell(CASHIER)
    check(len(shell.sidebar._buttons) == 1,
          "cashier sidebar shows ONLY Point of Sale")
    check(shell.active_key == "pos", "cashier lands directly on POS")
    check("products" not in shell.allowed_keys,
          "cashier cannot see the Products page")
    for forbidden in ("products", "menu", "recipe", "ingredients",
                      "inventory", "history", "monitoring", "calculator",
                      "dashboard", "users"):
        shell.navigate(forbidden)
        check(shell.active_key == "pos",
              f"cashier navigation to '{forbidden}' is refused")

    # A stray ProductsView must show the denied state and no data.
    shell.current_page.pack_forget()
    denied_view = products_page.ProductsView(shell.content, user=CASHIER)
    check(denied_view.allowed is False,
          "a stray Products page refuses a cashier")
    check(not hasattr(denied_view, "summary_frame"),
          "denied Products page builds no cards or table")
    denied_view.pack(fill="both", expand=True)
    pump()
    shot("stage12_05_cashier_products_denied.png")
    denied_view.destroy()
    shell.current_page.pack(fill="both", expand=True)
    pump(120)

    pos = set_pos_offline(shell)
    check(isinstance(pos, pos_page.PosView),
          "live POS is the Stage 12 barcode basket screen")
    check(pos.can_void is False and pos.is_manager is False,
          "cashier may not void sales or read gross-sales totals")
    check(str(pos.gross_label.cget("text")) == "",
          "gross-sales total is hidden from the cashier")
    check("Barcode scanner" in all_text(pos),
          "POS shows the barcode scanner entry")
    check(pos.confirm_button.cget("state") == "disabled",
          "Confirm sale starts disabled with an empty basket")
    shot("stage12_06_cashier_pos_empty.png")

    # ---- Scanner: first product by direct call, second by <Return> ----
    pos.scan_entry.insert(0, COKE)
    pos._add_by_barcode()
    pump(120)
    check(len(pos.cart) == 1, "scanning a barcode adds one basket line")
    check(pos.total_amount == 35.0, "Coca-Cola is priced at PHP 35.00")
    check(str(pos.total_label.cget("text")) == format_money(35.0),
          "total label uses the shared peso formatter")

    pos.scan_entry.insert(0, WATER)
    pos.scan_entry.focus_set()
    pos.scan_entry.event_generate("<Return>")
    pump(150)
    check(len(pos.cart) == 2,
          "the real <Return> binding acts like a USB barcode scanner")
    check(pos.total_amount == 50.0,
          "two scanned products total PHP 50.00")

    # Unknown barcode is rejected and the basket is unchanged.
    pos.scan_entry.delete(0, "end")
    pos.scan_entry.insert(0, "000000")
    pos._add_by_barcode()
    pump(100)
    check("No sellable product found" in str(pos.status_label.cget("text")),
          "an unknown barcode shows a friendly error")
    check(len(pos.cart) == 2, "unknown barcode adds nothing to the basket")
    check(pos.scan_entry.get() == "",
          "a rejected barcode is cleared so the next scan cannot concatenate")

    # Quantity adjustment, exact tender, and the cart screenshot.
    pos._change_qty(COKE, 1)
    pump(120)
    check(pos.total_amount == 85.0,
          "2 x Coca-Cola + 1 x water totals PHP 85.00")
    check(pos.can_confirm is True
          and str(pos.confirm_button.cget("state")) == "normal",
          "Confirm sale enables when stock is sufficient")
    pos._use_exact_total()
    check(pos.tendered_entry.get().replace(",", "") in ("85", "85.0", "85.00"),
          "Use exact total fills the tendered amount")
    check("3 item(s) in 2 line(s)" in str(pos.item_count_label.cget("text")),
          "basket item and line counts are correct")
    shot("stage12_07_cashier_pos_basket.png")

    # ---- Cancelling before confirm writes nothing ---------------------
    sales_before = scalar("SELECT COUNT(*) FROM store_sales")
    pos._clear()
    pump(120)
    check(len(pos.cart) == 0, "Clear basket empties the basket")
    check(scalar("SELECT COUNT(*) FROM store_sales") == sales_before,
          "cancelling a basket writes no sale")
    check(scalar("SELECT COUNT(*) FROM stock_out_details") == 0,
          "cancelling a basket deducts no stock")

    # ---- A real committed sale ----------------------------------------
    pos.scan_entry.insert(0, COKE)
    pos._add_by_barcode()
    pos.scan_entry.insert(0, WATER)
    pos.scan_entry.focus_set()
    pos.scan_entry.event_generate("<Return>")
    pos._change_qty(COKE, 1)
    pos._use_exact_total()
    pump(120)
    pos._confirm()
    pump(420)

    check(scalar("SELECT COUNT(*) FROM store_sales") == 1,
          "one sale was committed")
    check(scalar("SELECT total_amount FROM store_sales") == 85.0,
          "committed sale total is PHP 85.00")
    check(scalar("SELECT currency_code FROM store_sales") == "PHP",
          "sale records PHP as its currency")
    check(scalar("SELECT item_count FROM store_sales") == 3,
          "sale records 3 sold units")
    check(scalar("SELECT COUNT(*) FROM store_sale_items") == 2,
          "two immutable product snapshot lines were archived")
    check(scalar("SELECT COUNT(*) FROM stock_out_transactions") == 1,
          "one stock-out header was written")
    check(scalar("SELECT COUNT(*) FROM stock_out_details") == 2,
          "two stock-out detail lines were written")
    check(scalar("SELECT quantity_on_hand FROM ingredients "
                 "WHERE barcode = ?", (COKE,)) == 58,
          "Coca-Cola balance fell from 60 to 58")
    check(scalar("SELECT quantity_on_hand FROM ingredients "
                 "WHERE barcode = ?", (WATER,)) == 71,
          "water balance fell from 72 to 71")
    check(scalar("SELECT COUNT(*) FROM vw_stock_movements "
                 "WHERE movement_type = 'OUT'") == 2,
          "the sale is visible in the existing movement-history view")
    check(len(pos.cart) == 0, "basket clears after a confirmed sale")

    receipt_files = list(RECEIPTS.glob("*.txt"))
    check(len(receipt_files) == 1, "exactly one receipt was archived")
    if receipt_files:
        text = receipt_files[0].read_text(encoding="utf-8")
        check("Coca-Cola Original 500 ml" in text
              and "Pure Drinking Water 500 ml" in text,
              "receipt lists every scanned product")
        check(COKE in text and WATER in text,
              "receipt prints each product's barcode")
        check(format_money(85.0) in text and "PHP" in text,
              "receipt shows the PHP 85.00 total")
        check("\u00a5" not in text and "\uffe5" not in text
              and "$" not in text,
              "receipt has no yen/yuan/dollar sign")
        check("NEIGHBORHOOD MART" in text,
              "receipt is branded for the convenience store")
    shot("stage12_08_cashier_pos_after_sale.png")

    sale_id = scalar("SELECT sale_id FROM store_sales")
    # ---- View receipt and reprint -------------------------------------
    pos._view_sale(sale_id)
    receipt_dialog = dialog("receipt")
    # The receipt textbox is read-only; read it through the widget tree.
    boxes = []

    def _find_textbox(node):
        for child in node.winfo_children():
            if isinstance(child, ctk.CTkTextbox):
                boxes.append(child)
            _find_textbox(child)

    _find_textbox(receipt_dialog)
    if boxes:
        preview = boxes[0].get("0.0", "end")
        check("REPRINT COPY" in preview and CURRENCY_SYMBOL in preview,
              "receipt dialog shows the immutable reprint copy")
    shot("stage12_09_receipt_dialog.png", receipt_dialog)
    receipt_dialog.close()
    pump(120)

    pos._reprint(sale_id)
    pump(150)
    check(any("REPRINT" in path.name for path in RECEIPTS.glob("*.txt")),
          "Reprint archives a separate reprint copy")

    # ---- Cashier cannot void ------------------------------------------
    pos._void(sale_id)
    pump(100)
    check("Only a manager can void a sale" in str(pos.status_label.cget("text")),
          "cashier void attempt is refused in the UI")
    check("void" not in DIALOGS, "no Void dialog opens for a cashier")
    check(scalar("SELECT status FROM store_sales") == "completed",
          "the sale stays completed")

    # ==================================================================
    # MANAGER - void the sale and restore stock
    # ==================================================================
    shell.destroy()
    shell = build_shell(MANAGER)
    shell.navigate("pos")
    pump()
    manager_pos = set_pos_offline(shell)
    check(manager_pos.can_void is True,
          "manager has the void capability on POS")
    check("Gross sales" in str(manager_pos.gross_label.cget("text")),
          "manager can see the gross-sales total")

    manager_pos._void(sale_id)
    void_dialog = dialog("void")
    void_dialog.reason.insert(0, "Customer changed mind")
    void_dialog._save()
    pump(320)

    check(scalar("SELECT status FROM store_sales") == "voided",
          "voiding changes status instead of deleting the sale")
    check(scalar("SELECT COUNT(*) FROM store_sales") == 1,
          "voided sale remains in sales history")
    check(scalar("SELECT quantity_on_hand FROM ingredients "
                 "WHERE barcode = ?", (COKE,)) == 60,
          "Coca-Cola stock was returned to 60")
    check(scalar("SELECT quantity_on_hand FROM ingredients "
                 "WHERE barcode = ?", (WATER,)) == 72,
          "water stock was returned to 72")
    check(scalar("SELECT COUNT(*) FROM stock_in_details") == 21,
          "void added 2 compensating stock-in lines (18 + 1 + 2)")
    check(any("VOID" in path.name for path in RECEIPTS.glob("*.txt")),
          "a voided receipt copy was archived")
    check(manager_pos.gross_label.cget("text").endswith(format_money(0)),
          "gross sales excludes the voided sale")
    shot("stage12_10_manager_pos_after_void.png")

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
LOG_SIZE_AFTER = ERROR_LOG.stat().st_size if ERROR_LOG.exists() else -1
check(REAL_HASH_BEFORE == REAL_HASH_AFTER,
      "real database byte-for-byte unchanged")
check(LOG_SIZE_BEFORE == LOG_SIZE_AFTER, "no new crash-log entries")
check(not CAPTURE_FAILS,
      "all 10 screenshots rendered non-blank"
      + (f" (bad: {', '.join(CAPTURE_FAILS)})" if CAPTURE_FAILS else ""))
for relative in ("products.py", "store_sales.py", "receipt.py",
                 "pages/products.py", "pages/store_pos.py"):
    text = (ROOT / relative).read_text(encoding="utf-8")
    check("\u00a5" not in text and "\uffe5" not in text,
          f"no yen/yuan symbol in {relative}")

print("\n--- Stage 12 smoke summary ---")
print(f"PASS {len(PASS)}   FAIL {len(FAIL)}")
for item in FAIL:
    print("FAILED:", item)
print("receipts archived:", len(list(RECEIPTS.glob("*.txt"))))
print("screenshots:", len(list(OUT.glob("stage12_*.png"))))
TEMP.cleanup()
sys.exit(1 if FAIL else 0)
