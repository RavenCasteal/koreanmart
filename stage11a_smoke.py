"""
Stage 11a GUI smoke test (temporary verification script).

Opens the REAL CustomTkinter windows twice - once as a manager and once as a
cashier - and drives the real dialogs:

  manager: Menu page -> add "Spicy Ramen" -> Recipe page -> choose the new
           dish from the dropdown -> add an ingredient to ITS recipe
  cashier: still sees only POS, cannot open Menu, and a real sale still
           deducts ONLY the primary dish's 8 recipe lines

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
from ctypes import wintypes
from pathlib import Path

import customtkinter as ctk
from PIL import Image, ImageGrab

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import database
import menu_repository as dishes
import pages.menu as menu_page
import pages.pos as pos
import pages.recipe as recipe_page
import permissions
import receipt
import recipe_repository as recipes
import sales_repository as sales
import users_repository as users
from components.shell import AppShell
from components.theme import apply_appearance
from formatting import CURRENCY_SYMBOL

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

# A messagebox would block the non-interactive smoke run.
class _ConfirmYes:
    @staticmethod
    def askyesno(*args, **kwargs):
        return True


menu_page.messagebox = _ConfirmYes()

database.initialize_database(DB)
with database.transaction(DB) as conn:
    rows = conn.execute(
        """
        SELECT ri.ingredient_id, ri.quantity_per_serving
        FROM recipe_ingredients AS ri
        JOIN recipes AS r ON r.recipe_id = ri.recipe_id
        JOIN menu_items AS m ON m.menu_item_id = r.menu_item_id
        WHERE m.singleton = 1
        """
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


menu_page.DishDialog = _tracked("dish", menu_page.DishDialog)
recipe_page.AddRecipeIngredientDialog = _tracked(
    "add_line", recipe_page.AddRecipeIngredientDialog
)
pos.ConfirmSaleDialog = _tracked("confirm", pos.ConfirmSaleDialog)
pos.ReceiptDialog = _tracked("receipt", pos.ReceiptDialog)

# ---- window -------------------------------------------------------------
apply_appearance()
root = ctk.CTk()
root.title("Stage 11a GUI smoke test")
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
    """Capture a Tk toplevel directly with PrintWindow.

    ImageGrab reads the physical desktop, which is all black when this runs in
    a locked / virtual session. PrintWindow asks the window to paint itself
    into an offscreen bitmap, so it works without an active desktop.
    """
    user = ctypes.windll.user32
    gdi = ctypes.windll.gdi32
    hwnd = user.GetAncestor(win.winfo_id(), 2)  # GA_ROOT: the real toplevel
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
        header.biHeight = -height  # top-down rows
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


CAPTURE_FAILS: list[str] = []


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


try:
    # ==================================================================
    # MANAGER - the new Menu page and a second dish
    # ==================================================================
    shell = build_shell(MANAGER)
    check(len(shell.sidebar._buttons) == 10,
          "manager sidebar shows all 10 navigation items")
    check("menu" in shell.allowed_keys, "Menu is a manager-only page")
    check(shell.active_key == "dashboard", "manager lands on the Dashboard")
    check(permissions.current_session() is MANAGER,
          "shell installs the manager session actor")

    # Stage 11a sidebar layout: 10 pages must not clip the last item or the
    # exit footer at the default 1160x720 window.
    sidebar = shell.sidebar
    check(hasattr(sidebar, "nav_frame"),
          "navigation lives in a scrollable area (10 pages)")
    check(sidebar.nav_frame.winfo_height() > 200,
          "scrollable navigation has usable height")
    check(
        "Users" in [str(button.cget("text")).strip()
                    for button in sidebar._buttons.values()],
        "Users is still a reachable navigation item",
    )
    check(
        sidebar.exit_button.winfo_rooty() + sidebar.exit_button.winfo_height()
        <= root.winfo_rooty() + root.winfo_height(),
        "Exit application footer is visible at the default window size",
    )
    last_nav = list(sidebar._buttons.values())[-1]
    check(
        last_nav.winfo_rooty() + last_nav.winfo_height()
        <= sidebar.nav_frame.winfo_rooty() + sidebar.nav_frame.winfo_height(),
        "all 10 nav buttons fit without scrolling at 720px height",
    )

    shell.navigate("menu")
    pump()
    menu_view = shell.current_page
    check(shell.active_key == "menu", "manager can open the Menu page")
    check(menu_view.allowed is True, "Menu page opens for a manager")
    check(len(menu_view.summary_frame.winfo_children()) == 4,
          "Menu page shows 4 summary cards")
    check(len(menu_view.rows_frame.winfo_children()) == 1,
          "Menu table starts with just Regular Ramen")
    check("1 dish(es)" in str(menu_view.table_note.cget("text")),
          "table note reports 1 dish")
    shot("stage11a_01_manager_menu.png")

    # ---- Add a dish through the REAL dialog ---------------------------
    menu_view._add_dish()
    add_dish_dialog = dialog("dish")
    shot("stage11a_02_add_dish_dialog.png", add_dish_dialog)
    check(CURRENCY_SYMBOL in all_text(add_dish_dialog),
          "dish dialog labels prices in Philippine peso")
    add_dish_dialog.name.insert(0, "Spicy Ramen")
    add_dish_dialog.description.insert(0, "Extra chilli")
    add_dish_dialog.price.delete(0, "end")
    add_dish_dialog.price.insert(0, "180.00")
    add_dish_dialog._save()
    pump(200)

    rows = query("SELECT * FROM menu_items ORDER BY menu_item_id")
    check(len(rows) == 2, "a second dish now exists in SQLite")
    spicy_id = None
    if len(rows) == 2:
        spicy_id = int(rows[1]["menu_item_id"])
        check(rows[1]["name"] == "Spicy Ramen", "new dish name is saved")
        check(rows[1]["unit_price"] == 180.0, "new dish price is PHP 180.00")
        check(rows[1]["singleton"] == 0, "new dish is NOT the primary POS dish")
        check(rows[1]["is_active"] == 1, "new dish starts active")
    check(scalar("SELECT COUNT(*) FROM menu_items WHERE singleton = 1") == 1,
          "exactly one primary POS dish remains")
    check(scalar("SELECT COUNT(*) FROM recipes WHERE menu_item_id = ?",
                 (spicy_id,)) == 1,
          "the new dish got its own recipe row")
    check(scalar("SELECT COUNT(*) FROM recipe_ingredients") == 8,
          "the new recipe starts EMPTY (8 lines belong to Regular Ramen)")
    check(scalar("SELECT COUNT(*) FROM vw_stock_movements") == 0,
          "adding a dish wrote no stock movements")

    menu_view.refresh()
    pump()
    check(len(menu_view.rows_frame.winfo_children()) == 2,
          "Menu table refreshes to 2 dishes")
    check("2 dish(es)" in str(menu_view.table_note.cget("text")),
          "table note reports 2 dishes")
    shot("stage11a_03_menu_after_add.png")

    # ---- Editing the primary dish keeps its name locked --------------
    primary = [row for row in dishes.list_dishes(actor=MANAGER)
               if int(row["singleton"]) == 1][0]
    menu_view._edit_dish(primary)
    edit_dialog = dialog("dish")
    shot("stage11a_04_edit_primary_dialog.png", edit_dialog)
    check(str(edit_dialog.name.cget("state")) == "disabled",
          "primary dish name is locked until Stage 11b")
    edit_dialog.close()
    pump(100)

    # ---- Menu page's "Recipe screen" button really navigates ----------
    menu_view._go_recipe()
    pump()
    check(shell.active_key == "recipe", "Recipe screen button navigates")

    # ==================================================================
    # MANAGER - per-dish recipe editing
    # ==================================================================
    recipe_view = shell.current_page
    labels = list(recipe_view.dish_names)
    check(len(labels) == 2, "Recipe dish selector lists both active dishes")
    check(recipe_view.selected_id == int(primary["menu_item_id"]),
          "Recipe page opens on the primary POS dish")
    check(recipe_view.current_dish_name == "Regular Ramen",
          "primary recipe header shows Regular Ramen")
    check(len(query("SELECT * FROM recipe_ingredients")) == 8,
          "primary recipe still has 8 lines")
    shot("stage11a_05_recipe_primary.png")

    spicy_label = next(
        (label for label, key in recipe_view.dish_names.items()
         if key == spicy_id), None
    )
    check(spicy_label is not None, "new dish is selectable on the Recipe page")
    if spicy_label:
        recipe_view._on_dish_change(spicy_label)
        pump()
        check(recipe_view.current_dish_name == "Spicy Ramen",
              "switching the selector loads the new dish's recipe")
        check(recipe_view.selected_id == spicy_id, "selected dish id changed")
        check("This recipe has no ingredients yet" in all_text(recipe_view),
              "new dish shows the empty-recipe state")
        shot("stage11a_06_recipe_new_dish_empty.png")

        # Add ONE ingredient to the NEW dish through the real dialog.
        recipe_view._open_add_dialog()
        add_line_dialog = dialog("add_line")
        shot("stage11a_07_add_recipe_ingredient_dialog.png", add_line_dialog)
        pork_label = next(
            (value for value in add_line_dialog.combo.cget("values")
             if "Pork" in value), None
        )
        check(pork_label is not None, "Pork is available for the new recipe")
        if pork_label:
            add_line_dialog.combo.set(pork_label)
        add_line_dialog.quantity_entry.insert(0, "999")
        add_line_dialog._save()
        pump(200)

        check(scalar("SELECT COUNT(*) FROM recipe_ingredients") == 9,
              "one line was added to the new dish's recipe")
        lines_b = recipes.list_recipe_lines(DB, menu_item_id=spicy_id)
        check(len(lines_b) == 1 and lines_b[0]["quantity_per_serving"] == 999.0,
              "the new line is Pork 999 for Spicy Ramen ONLY")
        check(len(recipes.list_recipe_lines(DB)) == 8,
              "Regular Ramen's 8 recipe lines are unchanged")
        check(scalar("SELECT COUNT(*) FROM vw_stock_movements") == 0,
              "recipe edits still write no stock movements")
        recipe_view.refresh()
        pump()
        shot("stage11a_08_recipe_new_dish_built.png")

    # ---- Deactivation is a status change, reversible ------------------
    shell.navigate("menu")
    pump()
    menu_view = shell.current_page
    menu_view._toggle(
        {"menu_item_id": spicy_id, "name": "Spicy Ramen"}, True
    )
    pump(120)
    check(scalar("SELECT is_active FROM menu_items WHERE menu_item_id = ?",
                 (spicy_id,)) == 0,
          "deactivating hides the dish without deleting it")
    check(scalar("SELECT COUNT(*) FROM menu_items WHERE menu_item_id = ?",
                 (spicy_id,)) == 1,
          "deactivated dish row still exists")
    check(scalar("SELECT COUNT(*) FROM recipes WHERE menu_item_id = ?",
                 (spicy_id,)) == 1,
          "deactivated dish keeps its recipe")
    menu_view._toggle(
        {"menu_item_id": spicy_id, "name": "Spicy Ramen"}, False
    )
    pump(120)
    check(scalar("SELECT is_active FROM menu_items WHERE menu_item_id = ?",
                 (spicy_id,)) == 1,
          "reactivating restores the dish")

    # ==================================================================
    # CASHIER - still POS only, still sells ONLY Regular Ramen
    # ==================================================================
    shell.destroy()
    shell = build_shell(CASHIER)
    check(len(shell.sidebar._buttons) == 1,
          "cashier sidebar shows ONLY Point of Sale")
    check(shell.active_key == "pos", "cashier lands directly on POS")
    check(permissions.current_session() is CASHIER,
          "shell swaps the session actor to the cashier")
    check("menu" not in shell.allowed_keys, "cashier cannot see the Menu page")
    for forbidden in ("menu", "recipe", "ingredients", "inventory",
                      "history", "monitoring", "calculator", "dashboard",
                      "users"):
        shell.navigate(forbidden)
        check(shell.active_key == "pos",
              f"cashier navigation to '{forbidden}' is refused")

    # A stray MenuView must show the denied state and no data.
    shell.current_page.pack_forget()
    denied_view = menu_page.MenuView(shell.content, user=CASHIER)
    check(denied_view.allowed is False,
          "a stray Menu page refuses a cashier")
    check(not hasattr(denied_view, "summary_frame"),
          "denied Menu page builds no summary cards or dish table")
    denied_view.pack(fill="both", expand=True)
    pump()
    shot("stage11a_09_cashier_menu_denied.png")
    denied_view.destroy()
    shell.current_page.pack(fill="both", expand=True)
    pump(120)

    page = shell.current_page
    context = sales.get_pos_context(DB)
    check(context["menu_item_name"] == "Regular Ramen",
          "POS context still resolves the primary dish")
    check(context["unit_price"] == 150.0,
          "POS price is still PHP 150.00, not the new dish's 180.00")
    check(len(context["lines"]) == 8,
          "POS preview uses 8 lines, ignoring Spicy Ramen's Pork 999")
    check(999.0 not in [float(line["quantity_per_serving"])
                        for line in context["lines"]],
          "the 999 line never leaks into the POS preview")
    shot("stage11a_10_cashier_pos.png")

    # A real sale must deduct ONLY the primary recipe.
    page.servings_entry.delete(0, "end")
    page.servings_entry.insert(0, "2")
    page._use_exact_total()
    pump()
    check(page.tendered_entry.get() == "300",
          "Exact total = 2 x PHP 150.00 = 300")
    page._open_confirm()
    confirm_dialog = dialog("confirm")
    confirm_dialog._save()
    pump(520)
    receipt_dialog = dialog("receipt")
    receipt_text = receipt_dialog.textbox.get("0.0", "end")
    check("300.00" in receipt_text and CURRENCY_SYMBOL in receipt_text,
          "receipt shows the PHP 300.00 total")
    check("Regular Ramen" in receipt_text,
          "receipt still names Regular Ramen")
    check("\u00a5" not in receipt_text and "\uffe5" not in receipt_text,
          "receipt has no yen/yuan symbol")
    shot("stage11a_11_cashier_receipt.png", receipt_dialog)
    receipt_dialog.close()
    pump(150)

    deductions = query("SELECT * FROM sale_deductions")
    check(len(deductions) == 8, "cashier sale deducted exactly 8 lines")
    pork = [line for line in deductions if line["ingredient_name"] == "Pork"]
    check(len(pork) == 1 and pork[0]["quantity_deducted"] == 200.0,
          "Pork deduction = 2 x 100 grams, NOT 999 from the other dish")
    check(scalar("SELECT total_amount FROM sales_transactions") == 300.0,
          "committed sale total is PHP 300.00")
    check(scalar("SELECT COUNT(*) FROM menu_items WHERE singleton = 1") == 1,
          "one primary POS dish after the whole workflow")

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
      "all 11 screenshots rendered non-blank"
      + (f" (bad: {', '.join(CAPTURE_FAILS)})" if CAPTURE_FAILS else ""))
for relative in ("menu_repository.py", "pages/menu.py", "pages/recipe.py",
                 "database/schema.sql"):
    text = (ROOT / relative).read_text(encoding="utf-8")
    check("\u00a5" not in text and "\uffe5" not in text,
          f"no yen/yuan symbol in {relative}")

print("\n--- Stage 11a smoke summary ---")
print(f"PASS {len(PASS)}   FAIL {len(FAIL)}")
for item in FAIL:
    print("FAILED:", item)
print("receipts archived:", len(list((TMP / "receipts").glob("*.txt"))))
print("screenshots:", len(list(OUT.glob("stage11a_*.png"))))
TEMP.cleanup()
sys.exit(1 if FAIL else 0)
