"""
Stage 10 GUI smoke test (temporary verification script).

Opens the REAL CustomTkinter windows twice - once as a manager and once as a
cashier - and proves the shared counter PC shows the right screens to the
right person. It also re-checks the Stage 9 "Exact total" fix and confirms a
cashier can still complete a real sale.

ALL data is written to a temporary SQLite file: database.DB_PATH is
redirected before any widget is built, so the delivered database is never
touched. Printing is stubbed out. Screenshots go to assets/images/.
"""

from __future__ import annotations

import ctypes
import hashlib
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import customtkinter as ctk
from PIL import ImageGrab

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import database
import permissions
import receipt
import repository
import sales_repository as sales
import users_repository as users
from components.shell import AppShell
from components.theme import apply_appearance
import pages.pos as pos
import pages.users as users_page
from pages.users import UsersView

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

# Use the REAL authentication path for both sessions.
MANAGER = database.verify_credentials(
    database.DEFAULT_ADMIN_USERNAME, database.DEFAULT_ADMIN_PASSWORD, DB
)
assert MANAGER is not None
CASHIER_ID = users.create_user(
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


pos.ConfirmSaleDialog = _tracked("confirm", pos.ConfirmSaleDialog)
pos.ReceiptDialog = _tracked("receipt", pos.ReceiptDialog)
users_page.UserDialog = _tracked("add_user", users_page.UserDialog)

# ---- window -------------------------------------------------------------
apply_appearance()
root = ctk.CTk()
root.title("Stage 10 GUI smoke test")
root.geometry("1160x720+40+40")
root.configure(fg_color="#FAF6EF")

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
    small = image.convert("RGB").resize((80, 50))
    light = 0
    for r, g, b in small.getdata():
        if r > 225 and g > 218 and b > 205:
            light += 1
    return light / (80 * 50) > 0.25


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
    image.save(OUT / name)
    print(f"  SHOT  {name}  {image.size}")


def dialog(key: str):
    value = DIALOGS.pop(key, None)
    if value is None:
        raise RuntimeError(f"dialog {key} was not created")
    pump()
    return value


def set_servings(page, value: str) -> None:
    page.servings_entry.delete(0, "end")
    page.servings_entry.insert(0, value)


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
    # MANAGER session - all nine screens, including Users
    # ==================================================================
    shell = build_shell(MANAGER)
    check(len(shell.sidebar._buttons) == 9,
          "manager sidebar shows all 9 navigation items")
    check(shell.active_key == "dashboard", "manager lands on the Dashboard")
    check("Manager" in str(shell.header.user_label.cget("text")),
          "header identifies the signed-in Manager")
    check(permissions.current_session() is MANAGER,
          "shell installs the manager session actor")
    shot("stage10_01_manager_dashboard.png")

    shell.navigate("users")
    pump()
    users_view = shell.current_page
    check(users_view.allowed is True, "Users page opens for a manager")
    check(len(users_view.summary_frame.winfo_children()) == 4,
          "Users page shows 4 summary cards")
    shot("stage10_02_manager_users.png")

    # Create a second cashier through the real dialog.
    users_view._add_user()
    add_dialog = dialog("add_user")
    shot("stage10_03_manager_add_user_dialog.png", add_dialog)
    add_dialog.username.insert(0, "relief")
    add_dialog.full_name.insert(0, "Relief Cashier")
    add_dialog.password.insert(0, "Relief2026!")
    add_dialog.confirm.insert(0, "Relief2026!")
    add_dialog._save()
    pump(200)
    with database.transaction(DB) as conn:
        created = conn.execute(
            "SELECT role, is_active, must_change_password, password_hash "
            "FROM users WHERE username = 'relief'"
        ).fetchone()
    check(created is not None, "manager created a cashier through the dialog")
    if created is not None:
        check(created["role"] == "cashier", "new account has the cashier role")
        check(created["must_change_password"] == 1,
              "new account must set its own password at first login")
        check(created["password_hash"].startswith("pbkdf2_sha256$"),
              "temporary password is stored only as a PBKDF2 hash")
    shot("stage10_04_manager_users_after_add.png")

    # ==================================================================
    # CASHIER session - POS only, no management controls
    # ==================================================================
    shell.destroy()
    shell = build_shell(CASHIER)
    check(len(shell.sidebar._buttons) == 1,
          "cashier sidebar shows ONLY Point of Sale")
    check(shell.active_key == "pos", "cashier lands directly on POS")
    check("Cashier" in str(shell.header.user_label.cget("text")),
          "header identifies the signed-in Cashier")
    check(permissions.current_session() is CASHIER,
          "shell swaps the session actor to the cashier")
    page = shell.current_page

    check(page.price_button is None, "cashier has no Edit unit price button")
    check(str(page.confirm_button.cget("state")) == "disabled",
          "Confirm is disabled before a valid preview")
    check(
        str(page.confirm_button.cget("fg_color")) == pos.CONFIRM_DISABLED_COLOR,
        "disabled Confirm is visibly muted, not green",
    )
    check(len(page.summary_frame.winfo_children()) == 2,
          "cashier sees price + servings, NOT turnover totals")
    check(not permissions.can(CASHIER, permissions.CAP_VOID_SALE),
          "cashier cannot void a sale")
    shot("stage10_05_cashier_pos.png")

    # navigate() is the real gate, even for a page shortcut button.
    for forbidden in ("users", "ingredients", "recipe", "history",
                      "monitoring", "inventory", "calculator", "dashboard"):
        shell.navigate(forbidden)
        check(shell.active_key == "pos",
              f"cashier navigation to '{forbidden}' is refused")

    # ---- Stage 9 exact-total regression -------------------------------
    set_servings(page, "3")
    page._use_exact_total()
    pump()
    check(page.tendered_entry.get() == "450",
          "Exact total = 3 x PHP 150.00 = 450")
    check(page.preview is not None and page.preview["total_amount"] == 450.0,
          "first preview total is PHP 450.00")
    set_servings(page, "2")
    page._use_exact_total()
    pump()
    check(page.tendered_entry.get() == "300",
          "Exact total RECOMPUTES after servings change (stale-preview fix)")
    check(page.preview is not None and page.preview["total_amount"] == 300.0,
          "second preview total is PHP 300.00")
    check(
        str(page.confirm_button.cget("state")) == "normal"
        and str(page.confirm_button.cget("fg_color"))
        != pos.CONFIRM_DISABLED_COLOR,
        "Confirm turns green and enabled after a sufficient preview",
    )
    shot("stage10_06_cashier_exact_total.png")

    # ---- A cashier CAN still complete a real sale ----------------------
    page._open_confirm()
    confirm_dialog = dialog("confirm")
    shot("stage10_07_cashier_confirm_dialog.png", confirm_dialog)
    confirm_dialog._save()
    pump(520)
    receipt_dialog = dialog("receipt")
    receipt_text = receipt_dialog.textbox.get("0.0", "end")
    check("\u20b1300.00" in receipt_text,
          "cashier receipt shows the PHP 300.00 total")
    check("\u00a5" not in receipt_text and "\uffe5" not in receipt_text,
          "receipt has no yen/yuan symbol")
    shot("stage10_08_cashier_receipt.png", receipt_dialog)
    receipt_dialog.close()
    pump(150)

    with database.transaction(DB) as conn:
        sale = conn.execute(
            "SELECT sale_id, transaction_number, status, created_by, "
            "total_amount FROM sales_transactions"
        ).fetchone()
        deduction_count = conn.execute(
            "SELECT COUNT(*) FROM sale_deductions"
        ).fetchone()[0]
    check(sale is not None, "cashier sale was committed")
    if sale is not None:
        check(sale["status"] == "completed", "sale status is completed")
        check(sale["created_by"] == CASHIER["user_id"],
              "sale is attributed to the cashier account")
        check(abs(sale["total_amount"] - 300.0) < 1e-6,
              "committed total is PHP 300.00")
    check(deduction_count == 8, "8 recipe ingredients were deducted")

    # ---- A cashier CANNOT void, even by calling the UI method ----------
    if sale is not None:
        page._open_void(sale["sale_id"], sale["transaction_number"])
        pump(120)
        check("Only a manager" in str(page.status_label.cget("text")),
              "void is refused with a friendly message")
        check("void" not in DIALOGS, "no void dialog is opened for a cashier")
        with database.transaction(DB) as conn:
            status = conn.execute(
                "SELECT status FROM sales_transactions WHERE sale_id = ?",
                (sale["sale_id"],),
            ).fetchone()[0]
        check(status == "completed", "sale stays completed after a denied void")

    # ---- Repository guards hold with the cashier session live ----------
    for label, call in (
        ("change price", lambda: sales.set_unit_price(
            200, db_path=DB, actor=CASHIER)),
        ("add ingredient", lambda: repository.add_ingredient(
            "Sneaky", "grams", 1, "", db_path=DB, actor=CASHIER)),
        ("change price without actor", lambda: sales.set_unit_price(
            200, db_path=DB)),
    ):
        try:
            call()
            denied = False
        except permissions.PermissionDenied:
            denied = True
        except (ValueError, sqlite3.Error):
            denied = False
        check(denied, f"cashier cannot {label} at repository level")
    check(sales.get_unit_price(DB) == 150.0, "price stayed at PHP 150.00")

    # ---- Direct UsersView with a cashier shows the denied state --------
    shell.current_page.pack_forget()
    denied_view = UsersView(shell.content, user=CASHIER)
    check(denied_view.allowed is False,
          "a stray Users page refuses a cashier")
    denied_view.pack(fill="both", expand=True)
    pump()
    shot("stage10_09_cashier_users_denied.png")
    denied_view.destroy()
    shell.current_page.pack(fill="both", expand=True)
    pump(150)
    shot("stage10_10_cashier_final_pos.png")

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
print("\n--- Stage 10 smoke summary ---")
print(f"PASS {len(PASS)}   FAIL {len(FAIL)}")
for item in FAIL:
    print("FAILED:", item)
print("receipts archived:", len(list((TMP / "receipts").glob("*.txt"))))
print("screenshots:", len(list(OUT.glob("stage10_*.png"))))
TEMP.cleanup()
sys.exit(1 if FAIL else 0)
