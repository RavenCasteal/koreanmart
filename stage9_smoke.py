"""
Stage 9 GUI smoke test (temporary verification script).

Opens the REAL CustomTkinter windows, walks the whole Point of Sale flow and
captures screenshots into assets/images/. ALL data is written to a temporary
SQLite file - database.DB_PATH is redirected before any widget is built, so
the delivered database is never touched. Printing is stubbed out.
"""

from __future__ import annotations

import ctypes
import hashlib
import sys
import tempfile
import time
from pathlib import Path

import customtkinter as ctk
from PIL import ImageGrab

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import database
import receipt
import sales_repository as sales
import stock_repository as stock
from components.shell import AppShell
from components.theme import apply_appearance
import pages.pos as pos

OUT = ROOT / "assets" / "images"
OUT.mkdir(parents=True, exist_ok=True)
REAL_DB = ROOT / "database" / "ramen_inventory.db"
ERROR_LOG = ROOT / "logs" / "error.log"

TEMP = tempfile.TemporaryDirectory()
TMP = Path(TEMP.name)
DB = TMP / "ramen_inventory.db"

REAL_HASH_BEFORE = hashlib.sha256(REAL_DB.read_bytes()).hexdigest()
LOG_SIZE_BEFORE = ERROR_LOG.stat().st_size if ERROR_LOG.exists() else -1

# ---- redirect every write away from the delivered database -------------
database.DB_PATH = DB
receipt.RECEIPTS_DIR = TMP / "receipts"
receipt.print_receipt = lambda text, path=None, printer_name=None: {
    "method": "smoke_test",
    "detail": "Smoke test: receipt archived, printer not used.",
    "ok": True,
}

database.initialize_database(DB)
with database.transaction(DB) as conn:
    rows = conn.execute(
        "SELECT ingredient_id, quantity_per_serving FROM recipe_ingredients"
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE ingredients SET quantity_on_hand = ? WHERE ingredient_id = ?",
            (row["quantity_per_serving"] * 6, row["ingredient_id"]),
        )

# ---- track dialogs ------------------------------------------------------
DIALOGS: dict[str, object] = {}


class TrackedPrice(pos.PriceDialog):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        DIALOGS["price"] = self


class TrackedConfirm(pos.ConfirmSaleDialog):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        DIALOGS["confirm"] = self


class TrackedVoid(pos.VoidDialog):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        DIALOGS["void"] = self


class TrackedReceipt(pos.ReceiptDialog):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        DIALOGS["receipt"] = self


pos.PriceDialog = TrackedPrice
pos.ConfirmSaleDialog = TrackedConfirm
pos.VoidDialog = TrackedVoid
pos.ReceiptDialog = TrackedReceipt

# ---- window -------------------------------------------------------------
apply_appearance()
root = ctk.CTk()
root.title("Stage 9 GUI smoke test")
root.geometry("1160x720+40+40")
root.configure(fg_color="#FAF6EF")
shell = AppShell(
    root,
    user={"user_id": 1, "username": "admin",
          "full_name": "System Administrator"},
    on_logout=lambda: None,
    on_change_password=lambda: None,
    on_exit=lambda: None,
)
shell.pack(fill="both", expand=True)
root.update()

PASS: list[str] = []
FAIL: list[str] = []


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
    """Reject a screenshot that accidentally captured the desktop wallpaper."""
    small = image.convert("RGB").resize((80, 50))
    light = 0
    for r, g, b in small.getdata():
        if r > 225 and g > 218 and b > 205:  # cream / white surfaces
            light += 1
    return light / (80 * 50) > 0.25


def shot(name: str, target=None) -> None:
    win = target or root
    for _attempt in range(4):
        front(win, others=[root] if win is not root else ())
        time.sleep(0.28)
        win.update()
        x, y = win.winfo_rootx(), win.winfo_rooty()
        w, h = win.winfo_width(), win.winfo_height()
        image = ImageGrab.grab(bbox=(x, y, x + w, y + h))
        if looks_like_app(image):
            break
    else:
        image = ImageGrab.grab(bbox=(x, y, x + w, y + h))
    image.save(OUT / name)
    print(f"  SHOT  {name}  {image.size}")


def dialog(key: str):
    value = DIALOGS.pop(key, None)
    if value is None:
        raise RuntimeError(f"dialog {key} was not created")
    pump()
    return value


def set_servings(value: str) -> None:
    page.servings_entry.delete(0, "end")
    page.servings_entry.insert(0, value)


try:
    # 1 - Dashboard (the shell now has 8 pages) ----------------------------
    shell.navigate("dashboard")
    pump()
    check(len(shell.sidebar._buttons) == 8, "sidebar shows 8 navigation items")
    shot("stage9_01_dashboard.png")

    # 2 - POS initial state -------------------------------------------------
    shell.navigate("pos")
    pump()
    page = shell.current_page
    label = page.context_label.cget("text")
    check("\u20b1150.00" in label, "unit price displayed in Philippine peso")
    check("PHP" not in label or "\u20b1" in label, "no foreign currency sign")
    check(page.context["max_servings"] == 6, "6 servings available from seeded stock")
    check(str(page.confirm_button.cget("state")) == "disabled",
          "Confirm is disabled before a valid preview")
    shot("stage9_02_pos_initial.png")

    # 3 - Unit price dialog, then CANCEL (no write) -------------------------
    page._edit_price()
    price_dialog = dialog("price")
    shot("stage9_03_pos_price_dialog.png", price_dialog)
    price_dialog.entry.delete(0, "end")
    price_dialog.entry.insert(0, "175")
    price_dialog.close()
    pump(120)
    check(sales.get_unit_price(DB) == 150.0,
          "cancelling the price dialog changes nothing")

    # 4 - Sufficient preview -----------------------------------------------
    set_servings("2")
    page._use_exact_total()
    page._run_preview()
    pump()
    check(page.preview is not None and page.preview["sufficient"],
          "2 servings preview is sufficient")
    check(page.preview["total_amount"] == 300.0, "total = 2 x ₱150.00 = ₱300.00")
    check(str(page.confirm_button.cget("state")) == "normal",
          "Confirm enabled after a sufficient preview")
    shot("stage9_04_pos_preview_sufficient.png")

    # 5 - Insufficient preview ----------------------------------------------
    set_servings("99")
    page.tendered_entry.delete(0, "end")
    page.tendered_entry.insert(0, "999999")
    page._run_preview()
    pump()
    check(page.preview is not None and not page.preview["sufficient"],
          "99 servings preview is blocked by insufficient stock")
    check(str(page.confirm_button.cget("state")) == "disabled",
          "Confirm disabled when stock is insufficient")
    shot("stage9_05_pos_preview_insufficient.png")

    # 6 - Confirmation dialog ------------------------------------------------
    set_servings("2")
    page._use_exact_total()
    page._run_preview()
    pump()
    page._open_confirm()
    confirm_dialog = dialog("confirm")
    shot("stage9_06_pos_confirm_dialog.png", confirm_dialog)

    # 7 - Commit + receipt ---------------------------------------------------
    confirm_dialog._save()
    pump(520)
    receipt_dialog = dialog("receipt")
    receipt_text = receipt_dialog.textbox.get("0.0", "end")
    check("\u20b1300.00" in receipt_text, "receipt shows the ₱300.00 total")
    check("\u00a5" not in receipt_text and "\uffe5" not in receipt_text,
          "receipt has no yen/yuan symbol")
    for field in ("RR-", "Cash", "Tendered", "Change", "PHP"):
        check(field in receipt_text, f"receipt contains {field}")
    shot("stage9_07_pos_receipt.png", receipt_dialog)
    receipt_dialog.close()
    pump(120)

    history = sales.get_sales_history(db_path=DB)
    check(history["summary"]["total_sales"] == 1, "one sale row committed")
    check(history["summary"]["gross_sales"] == 300.0, "gross sales = ₱300.00")
    with database.transaction(DB) as conn:
        deductions = conn.execute(
            "SELECT COUNT(*) FROM sale_deductions"
        ).fetchone()[0]
        movements = conn.execute(
            "SELECT COUNT(*) FROM vw_stock_movements WHERE reason = 'Sale'"
        ).fetchone()[0]
    check(deductions == 8, "8 immutable deduction snapshots")
    check(movements == 8, "8 stock-out lines visible in the movement view")
    shot("stage9_08_pos_after_sale.png")

    # 8 - Recent sales card (scrolled) ---------------------------------------
    canvas = getattr(page.scroller, "_parent_canvas", None)
    if canvas is not None:
        canvas.yview_moveto(1.0)
        pump(200)
    shot("stage9_09_pos_sales_history.png")
    if canvas is not None:
        canvas.yview_moveto(0.0)
        pump(150)

    # 9 - Void dialog + voided receipt ---------------------------------------
    sale_id = history["rows"][0]["sale_id"]
    number = history["rows"][0]["transaction_number"]
    page._open_void(sale_id, number)
    void_dialog = dialog("void")
    shot("stage9_10_pos_void_dialog.png", void_dialog)
    void_dialog.reason.insert(0, "Customer cancelled")
    void_dialog._save()
    pump(520)
    void_receipt_dialog = dialog("receipt")
    void_text = void_receipt_dialog.textbox.get("0.0", "end")
    check("VOIDED" in void_text and "SALE CANCELLED" in void_text,
          "voided receipt is clearly marked")
    shot("stage9_11_pos_void_receipt.png", void_receipt_dialog)
    void_receipt_dialog.close()
    pump(150)

    void_summary = sales.get_sales_history(db_path=DB)["summary"]
    check(void_summary["voided_sales"] == 1, "sale marked voided, not deleted")
    check(void_summary["gross_sales"] == 0.0, "voided sale excluded from gross")
    with database.transaction(DB) as conn:
        restored = conn.execute(
            "SELECT quantity_on_hand FROM ingredients WHERE name = 'Pork'"
        ).fetchone()[0]
    check(abs(restored - 600.0) < 1e-6,
          "void restores stock to the original 6 servings")
    shot("stage9_12_pos_after_void.png")

    # 10 - Existing pages see the sale / reversal ----------------------------
    shell.navigate("history")
    pump()
    sale_movements = stock.get_history("OUT", "Sale", db_path=DB)
    check(sale_movements["filtered"] == 8,
          "Inventory History filters POS deductions by reason 'Sale'")
    shot("stage9_13_history_sale_movement.png")

    shell.navigate("monitoring")
    pump()
    shot("stage9_14_monitoring_after_sale.png")

    shell.navigate("calculator")
    pump()
    shot("stage9_15_calculator_still_readonly.png")

    shell.navigate("pos")
    pump(200)
    shot("stage9_16_final_pos.png")

finally:
    try:
        root.destroy()
    except Exception:
        pass

REAL_HASH_AFTER = hashlib.sha256(REAL_DB.read_bytes()).hexdigest()
LOG_SIZE_AFTER = ERROR_LOG.stat().st_size if ERROR_LOG.exists() else -1
check(REAL_HASH_BEFORE == REAL_HASH_AFTER,
      "real database byte-for-byte unchanged")
check(LOG_SIZE_BEFORE == LOG_SIZE_AFTER,
      "no new crash-log entries")
print("\n--- smoke summary ---")
print(f"PASS {len(PASS)}   FAIL {len(FAIL)}")
for item in FAIL:
    print("FAILED:", item)
print("receipts archived:", len(list((TMP / "receipts").glob("*.txt"))))
TEMP.cleanup()
sys.exit(1 if FAIL else 0)
