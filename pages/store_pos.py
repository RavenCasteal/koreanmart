"""
Stage 12 - Point of Sale for a convenience store (barcode scan to ring up).

This replaces the old "number of ramen servings" POS. A cashier scans a
product with a USB barcode scanner (or types the digits), the product joins a
basket, and ONE Confirm commits the whole sale: every product's stock is
deducted atomically, the money is recorded and a PHP receipt is archived and
printed AFTER the commit.

Cashier flow:
    1. Click into the Scan field (the scanner acts as a keyboard and presses
       Enter automatically).
    2. Repeat for every item; adjust quantities with - / + or Remove.
    3. Choose the payment method, then TAP the bill the customer handed over
       (or type the exact cash received). The change is worked out instantly.
    4. Press Confirm sale. A receipt is printed; the basket clears.

Safety rules:
  * Cancel / Clear before Confirm writes NOTHING to the database.
  * Only POS deducts stock; the usage calculator stays read-only.
  * Void (manager only) keeps the original sale and adds a compensating
    stock-in. History is never deleted.
  * A cashier may record a sale and reprint a receipt, but cannot see gross
    sales totals, change prices or void a sale.
  * All money is Philippine peso (PHP / ₱).

All SQL lives in store_sales.py / products.py. This file only builds the UI.

Stage 14 - hardware input / output
----------------------------------
  * Barcode entry is handled HERE, not in main.py (which is only the app
    entry point). A USB HID scanner is a keyboard: it types the digits of a
    code in a few milliseconds and then sends Enter. scanner.py owns the
    rules (burst detection, validation, debounce); this view owns the focus.
  * A root-level <Key> binding watches for scanner bursts even when focus has
    drifted to another field, redirects the burst into the scan field, and a
    low-frequency focus guard returns the caret there when the counter is
    idle. The cashier never has to click the scan field repeatedly.
  * Printer setup opens a small dialog listing installed Windows printers
    (win32print.EnumPrinters) - no printer name is hardcoded. Receipts are
    archived first and printed after the sale has committed.

Stage 15 - Senior/PWD discounts and cashier calculator
------------------------------------------------------
  * A single 20% Senior Citizen or PWD discount may be applied to the whole
    basket. The customer's name and ID number are REQUIRED and are stored as
    an immutable snapshot with the sale (and printed on the receipt).
  * discounts.py owns the money rules; calculator.py owns the four-function
    arithmetic. This view only builds widgets and calls into them.
  * An embedded calculator keypad lets the cashier work out change / totals;
    its result can be sent straight into Amount tendered.

Stage 16 - automatic change ("auto deduct")
--------------------------------------------
  * The cashier NEVER calculates change. change.py subtracts the NET total
    from the cash received and shows "Change due" live, plus a suggested
    bill/coin breakdown, the moment the tendered amount changes.
  * One-tap Quick cash buttons cover the common PHP bills. The tendered
    entry is bound to a StringVar, so typing, quick-cash, "Use exact total"
    and "Use result as amount tendered" ALL refresh the change instantly.
  * This is DISPLAY ONLY. store_sales.confirm_sale() still recomputes the
    total and change inside BEGIN IMMEDIATE; a stale UI cannot alter a sale.
  * can_confirm deliberately keeps its old meaning (basket + discount
    validity). An underpaid sale is shown in red AND rejected by the
    repository, so no partial sale can ever be committed.

Stage 17 - compact counter layout
----------------------------------
  * The whole checkout flow (scan, basket, TOTAL, live CHANGE, quick cash,
    tendered, Confirm) fits inside 1160x720 with NO scrolling. Stage 16 made
    mental arithmetic unnecessary, so the calculator and the Senior/PWD
    discount controls now sit behind one clearly labelled collapsible section;
    Sales history sits behind another. Nothing is removed - both open with a
    single click and every widget keeps its old attribute name.
  * CTkFrame defaults to 200px tall even when empty. Dynamic containers
    (basket rows, history rows, section bodies) MUST be built with height=1
    or an empty basket silently reserves 200px of dead space.
"""

from __future__ import annotations

import sqlite3
import time
from tkinter import StringVar, messagebox
from typing import Any, Callable

import customtkinter as ctk

import calculator
import change
import discounts
import permissions
import products
import receipt
import scanner
import store_sales
from components.dialog import ModalDialog
from components.theme import (
    ACCENT,
    ACCENT_HOVER,
    BORDER,
    CARD,
    CREAM,
    DANGER,
    PANEL,
    SUCCESS,
    TEXT,
    TEXT_MUTED,
    WARNING,
    font,
)
from formatting import CURRENCY_SYMBOL, format_money, format_number

CONFIRM_DISABLED_COLOR = "#D9D2C6"
# grid "uniform" scales every column by the largest minsize/weight ratio.
# Keeping weight ~= minsize/10 stops a narrow column from inflating the
# whole table past the 1160x720 window (which clipped the action buttons).
CART_WEIGHTS = (20, 15, 7, 10, 11, 17)
CART_MINSIZES = (200, 145, 70, 95, 105, 165)
CART_TITLES = ("Product", "Barcode", "Qty", "Price", "Line total", "Actions")
HISTORY_WEIGHTS = (15, 14, 7, 9, 9, 7, 19)
HISTORY_MINSIZES = (150, 135, 65, 90, 85, 70, 190)
HISTORY_TITLES = (
    "Transaction", "Date / time", "Items", "Total", "Payment", "Status", "Actions",
)

# Stage 14: how often to check that the scan field still owns the keyboard,
# and how long a cashier may type in another field before we take it back.
FOCUS_GUARD_MS = 900
IDLE_REFOCUS_MS = 2000
PRINTER_LABEL_DEFAULT = "Windows default printer"
PRINTER_LABEL_ARCHIVE = "Archive only (no printing)"
PRINTER_LABEL_PREFIX = "ESC/POS: "


class ReceiptDialog(ModalDialog):
    """Read-only receipt preview used by the View button in Sales history."""

    def __init__(self, master: Any, text: str) -> None:
        super().__init__(master, "Receipt", 520)
        self.heading("Archived receipt", "This is the immutable snapshot of the sale.")
        box = ctk.CTkTextbox(
            self.card,
            height=380,
            corner_radius=10,
            fg_color=PANEL,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT,
            font=("Consolas", 11),
        )
        box.pack(fill="both", expand=True, padx=24, pady=(0, 8))
        box.insert("1.0", text)
        box.configure(state="disabled")
        self.actions("Close", self.close, cancel_text=None)
        self.finish()


class VoidDialog(ModalDialog):
    """Manager-only reason prompt for voiding a completed sale."""

    def __init__(
        self, master: Any, on_saved: Callable[[str], None]
    ) -> None:
        super().__init__(master, "Void sale", 460)
        self.on_saved = on_saved
        self.heading(
            "Void this sale?",
            "The original receipt stays in history and the sold products are "
            "returned to stock as a stock-in movement.",
        )
        self.reason = self.field("Reason", on_enter=self._save)
        self.error_label()
        self.actions("Void sale", self._save, danger=True)
        self.finish(self.reason)

    def _save(self) -> None:
        try:
            reason = store_sales.validate_void_reason(self.reason.get())
        except ValueError as exc:
            self.show_error(str(exc))
            return
        self.close()
        self.on_saved(reason)


class PrinterDialog(ModalDialog):
    """Select the local receipt printer. Nothing here hardcodes a device.

    The list comes from win32print.EnumPrinters(), so any ESC/POS-compatible
    USB printer that Windows has installed can be chosen. "Windows default
    printer" keeps the original Stage 9 os.startfile() behaviour; "Archive
    only" is useful while the printer is unplugged.
    """

    def __init__(
        self, master: Any, on_status: Callable[..., None] | None = None
    ) -> None:
        super().__init__(master, "Receipt printer", 500)
        self.on_status = on_status
        settings = receipt.printer_settings()
        self.config = settings["config"]
        self.names = list(settings["printers"])
        self.heading(
            "Receipt printer",
            "Send receipts straight to a 58 mm USB thermal printer. The "
            "receipt is always archived first, so a printer problem never "
            "loses a sale.",
        )

        if not settings["windows"]:
            note = "Direct ESC/POS printing requires Windows."
        elif not settings["pywin32"]:
            note = (
                "pywin32 is not installed - run .venv\\Scripts\\python.exe "
                "-m pip install pywin32 to see USB printers."
            )
        elif not self.names:
            note = "No printers were found. Connect the printer and install its driver."
        else:
            note = (
                f"{len(self.names)} Windows printer(s) found. Default: "
                f"{settings['default'] or 'none'}"
            )
        ctk.CTkLabel(
            self.card, text=note, font=font(11), text_color=TEXT_MUTED,
            wraplength=400, justify="left", anchor="w",
        ).pack(fill="x", padx=24, pady=(0, 10))

        self.choice = ctk.CTkComboBox(
            self.card, values=self._labels(), height=38, corner_radius=9,
            font=font(12),
        )
        self.choice.set(self._current_label())
        self.choice.pack(fill="x", padx=24, pady=(0, 12))

        self.auto_var = ctk.IntVar(value=1 if self.config["auto_print"] else 0)
        ctk.CTkCheckBox(
            self.card,
            text="Print automatically after each completed sale",
            variable=self.auto_var, font=font(12), text_color=TEXT,
        ).pack(anchor="w", padx=24, pady=(0, 6))
        self.cut_var = ctk.IntVar(value=1 if self.config["cut"] else 0)
        ctk.CTkCheckBox(
            self.card,
            text="Cut paper after printing",
            variable=self.cut_var, font=font(12), text_color=TEXT,
        ).pack(anchor="w", padx=24, pady=(0, 8))
        self.error_label()

        ctk.CTkButton(
            self.card, text="Test print (no sale is created)",
            command=self._test_print, height=34, corner_radius=9,
            fg_color=PANEL, hover_color="#F0E9DD",
            border_width=1, border_color=BORDER,
            text_color=TEXT_MUTED, font=font(11),
        ).pack(fill="x", padx=24, pady=(2, 6))
        self.actions("Save", self._save, cancel_text="Cancel")
        self.finish()

    def _labels(self) -> list[str]:
        return (
            [PRINTER_LABEL_DEFAULT, PRINTER_LABEL_ARCHIVE]
            + [PRINTER_LABEL_PREFIX + name for name in self.names]
        )

    def _current_label(self) -> str:
        if self.config["mode"] == "archive_only":
            return PRINTER_LABEL_ARCHIVE
        if self.config["mode"] == "escpos" and self.config["printer_name"]:
            return PRINTER_LABEL_PREFIX + self.config["printer_name"]
        return PRINTER_LABEL_DEFAULT

    def _selection(self) -> tuple[str, str]:
        chosen = self.choice.get().strip()
        if chosen == PRINTER_LABEL_ARCHIVE:
            return "archive_only", ""
        if chosen.startswith(PRINTER_LABEL_PREFIX):
            return "escpos", chosen[len(PRINTER_LABEL_PREFIX):].strip()
        return "default", ""

    def _status(self, text: str, *, error: bool = False) -> None:
        if self.on_status is not None:
            self.on_status(text, error=error)

    def _test_print(self) -> None:
        mode, name = self._selection()
        if mode != "escpos" or not name:
            self.show_error("Choose an ESC/POS printer first, then test it.")
            return
        outcome = receipt.print_test_page(name)
        self.show_error("" if outcome["ok"] else outcome["detail"])
        self._status(outcome["detail"], error=not outcome["ok"])

    def _save(self) -> None:
        mode, name = self._selection()
        if mode == "escpos" and not name:
            self.show_error("Choose an installed ESC/POS printer first.")
            return
        try:
            receipt.save_printer_config(
                {
                    "mode": mode,
                    "printer_name": name,
                    "auto_print": bool(self.auto_var.get()),
                    "cut": bool(self.cut_var.get()),
                }
            )
        except OSError as exc:
            self.show_error(f"Could not save printer settings: {exc}")
            return
        if mode == "escpos":
            status = receipt.check_printer(name)
            detail = (
                f"Receipt printer set to {name}. {status['detail']}"
                if status["available"]
                else f"Receipt printer set to {name}, but: {status['detail']}"
            )
        elif mode == "archive_only":
            detail = "Printing is off; receipts are archived only."
        else:
            detail = "Receipts will use the Windows default printer."
        self.close()
        self._status(detail, error=False)


class PosView(ctk.CTkFrame):
    """Scan-to-ring-up Point of Sale. Both roles see exactly this screen."""

    def __init__(
        self,
        master: Any,
        user: dict[str, Any] | None = None,
        on_navigate: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(master, fg_color=CREAM, corner_radius=0)
        self.user = user or {}
        self.actor = self.user
        self.on_navigate = on_navigate
        self.dialog: ModalDialog | None = None
        self.cart: list[dict[str, Any]] = []
        self.products_by_label: dict[str, str] = {}
        self.total_amount = 0.0
        self.can_confirm = False
        # Stage 16: ONE source of truth for the cash received. A write trace
        # refreshes the auto-calculated change for every input path.
        self.tendered_var = StringVar(master=self, value="")
        # Tests / smoke runs turn printing off; the receipt is still archived.
        self.auto_print = True
        self.receipt_dir: Any = None

        # Stage 15: pure arithmetic state, shared with the embedded keypad.
        self.calc = calculator.Calculator()

        # Stage 14 scanner state. The buffer lives in scanner.py so the exact
        # same rules can be exercised without a display in unit tests.
        self.scan_buffer = scanner.ScanBuffer()
        self.scan_debouncer = scanner.ScanDebouncer()
        self._burst_widget: Any = None
        self._last_human_key_ms = 0.0
        self._key_funcid: str | None = None
        self._guard_after: str | None = None
        self._scanner_root: Any = None
        self._destroy_funcid: str | None = None
        self._confirming = False

        self.can_void = permissions.can(self.user, permissions.CAP_VOID_SALE)
        self.is_manager = permissions.is_manager(self.user)

        # Stage 17 - collapsible secondary sections. Each section owns its
        # OWN frame, so packing/unpacking its body always keeps the correct
        # order (toggle first, body second) without reordering siblings.
        self._sections: dict[str, dict[str, Any]] = {}

        self.scroller = ctk.CTkScrollableFrame(
            self, fg_color=CREAM, corner_radius=0
        )
        self.scroller.pack(fill="both", expand=True)
        self.container = ctk.CTkFrame(self.scroller, fg_color="transparent")
        self.container.pack(fill="both", expand=True, padx=18, pady=5)

        self._build_header()
        split = ctk.CTkFrame(self.container, fg_color="transparent")
        split.pack(fill="x")
        self._build_scan_panel(split)
        self._build_payment_panel(split)
        # Stage 17: ONE compact toggle bar for the two secondary sections.
        # Their expanded bodies share one area at the bottom of the page.
        self._toggle_bar = ctk.CTkFrame(
            self.container, fg_color="transparent", height=1
        )
        self._toggle_bar.pack(fill="x", pady=(5, 0))
        self._section_area = ctk.CTkFrame(
            self.container, fg_color="transparent", height=1
        )
        self._section_area.pack(fill="x")
        self._build_tools()
        self._build_history()
        self.refresh()
        self._install_scanner_focus()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _build_header(self) -> None:
        head = ctk.CTkFrame(self.container, fg_color="transparent")
        head.pack(fill="x", pady=(0, 4))
        ctk.CTkLabel(
            head, text="Point of Sale", font=font(17, "bold"), text_color=TEXT
        ).pack(side="left")
        ctk.CTkLabel(
            head,
            text="Scan → basket → take cash → Confirm  ·  PHP / "
                 + CURRENCY_SYMBOL,
            font=font(10), text_color=TEXT_MUTED,
        ).pack(side="right")
        self.status_label = ctk.CTkLabel(
            self.container,
            text="",
            font=font(11),
            text_color=SUCCESS,
            anchor="w",
            wraplength=840,
            justify="left",
            height=18,
        )
        self.status_label.pack(fill="x")

    def _card(self, parent: Any, title: str) -> ctk.CTkFrame:
        panel = ctk.CTkFrame(
            parent, fg_color=CARD, corner_radius=12,
            border_width=1, border_color=BORDER,
        )
        ctk.CTkLabel(
            panel, text=title, font=font(12, "bold"), text_color=TEXT,
            anchor="w", height=20,
        ).pack(fill="x", padx=14, pady=(6, 0))
        return panel

    # ------------------------------------------------------------------
    # Stage 17 - collapsible sections (compact, but nothing is removed)
    # ------------------------------------------------------------------
    def _section(self, name: str, closed: str, opened: str) -> ctk.CTkFrame:
        """Build a labelled toggle + an initially hidden body frame.

        The body lives INSIDE its own section frame, so re-packing it always
        puts it directly under its toggle regardless of what else is packed
        in self.container. height=1 is essential: CTkFrame defaults to 200px
        and an empty/hidden section would otherwise eat a fifth of the screen.
        """
        toggle = ctk.CTkButton(
            self._toggle_bar,
            text=closed,
            command=lambda: self.toggle_section(name),
            height=26,
            corner_radius=8,
            fg_color=PANEL,
            hover_color="#F0E9DD",
            border_width=1,
            border_color=BORDER,
            text_color=TEXT_MUTED,
            font=font(10, "bold"),
            anchor="w",
        )
        toggle.pack(side="left", fill="x", expand=True, padx=(0, 5))
        body = ctk.CTkFrame(
            self._section_area, fg_color="transparent", height=1
        )
        self._sections[name] = {
            "toggle": toggle, "body": body, "open": False,
            "closed": closed, "opened": opened,
        }
        return body

    def set_section(self, name: str, open_it: bool) -> None:
        """Show/hide a section. Used by the GUI smoke tests and the toggles."""
        info = self._sections[name]
        if bool(info["open"]) == bool(open_it):
            return
        if open_it:
            info["body"].pack(fill="x", pady=(6, 0))
        else:
            info["body"].pack_forget()
        info["open"] = bool(open_it)
        info["toggle"].configure(
            text=info["opened"] if open_it else info["closed"]
        )
        # CTkFrame does NOT shrink when a packed child is forgotten - it keeps
        # the old height and silently reintroduces the very scrollbar we just
        # removed. Reset it; pack propagation grows it again for any section
        # that is still open.
        self._section_area.configure(height=1)
        self.update_idletasks()

    def toggle_section(self, name: str) -> None:
        self.set_section(name, not self._sections[name]["open"])

    def open_tools(self) -> None:
        """Reveal the discount + calculator cards (one click for a cashier)."""
        self.set_section("tools", True)

    def open_history(self) -> None:
        """Reveal Sales history (one click for a cashier)."""
        self.set_section("history", True)

    def _build_scan_panel(self, parent: Any) -> None:
        panel = self._card(parent, "1  ·  Scan products & basket")
        panel.pack(fill="x")

        label_row = ctk.CTkFrame(panel, fg_color="transparent")
        label_row.pack(fill="x", padx=14, pady=(3, 1))
        ctk.CTkLabel(
            label_row,
            text="Barcode scanner / manual entry - scan or type, then press Enter",
            font=font(10, "bold"), text_color=TEXT_MUTED, anchor="w", height=20,
        ).pack(side="left")
        # Stage 14: local device setting, available to both roles. It only
        # writes config/printer.json - no business permission is involved.
        ctk.CTkButton(
            label_row,
            text="Printer setup",
            command=self._open_printer_dialog,
            width=104, height=26, corner_radius=7,
            fg_color=PANEL, hover_color="#F0E9DD",
            border_width=1, border_color=BORDER,
            text_color=TEXT_MUTED, font=font(10),
        ).pack(side="right")
        scan_row = ctk.CTkFrame(panel, fg_color="transparent")
        scan_row.pack(fill="x", padx=14)
        # Stage 14: NO placeholder on this field. CTkEntry.get() returns ""
        # while its placeholder mode is active - and a HID scanner can type
        # in the same instant the window regains focus, before the queued
        # FocusIn deactivates it. A lost first scan is unacceptable at a
        # checkout counter, so the hint lives in the label above instead.
        self.scan_entry = ctk.CTkEntry(
            scan_row,
            height=38,
            corner_radius=10,
            fg_color=PANEL,
            border_width=1,
            border_color=ACCENT,
            text_color=TEXT,
            font=font(15),
        )
        self.scan_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.scan_entry.bind("<Return>", lambda _event: self._add_by_barcode())
        ctk.CTkButton(
            scan_row,
            text="Add",
            command=self._add_by_barcode,
            width=76,
            height=38,
            corner_radius=10,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            text_color="#FFFFFF",
            font=font(13, "bold"),
        ).pack(side="left")

        # Stage 17: the "damaged barcode" hint lives in the placeholder so the
        # name-search row costs ONE row instead of two.
        pick_row = ctk.CTkFrame(panel, fg_color="transparent")
        pick_row.pack(fill="x", padx=14, pady=(3, 0))
        self.picker_search = ctk.CTkEntry(
            pick_row,
            placeholder_text="Damaged barcode? Type a product name...",
            height=30,
            corner_radius=9,
            fg_color=PANEL,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT,
            font=font(12),
            width=190,
        )
        self.picker_search.pack(side="left", padx=(0, 8))
        self.picker_search.bind("<KeyRelease>", lambda _event: self._reload_picker())
        self.picker_combo = ctk.CTkComboBox(
            pick_row,
            values=["No products found"],
            width=210,
            height=30,
            corner_radius=9,
        )
        self.picker_combo.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkButton(
            pick_row,
            text="Add",
            command=self._add_selected,
            width=64,
            height=30,
            corner_radius=9,
            fg_color=PANEL,
            hover_color="#F0E9DD",
            border_width=1,
            border_color=BORDER,
            text_color=TEXT,
            font=font(11),
        ).pack(side="left")

        # Stage 17: "Basket" is now part of the card title, so the table header
        # follows the name-search row directly with no spare label row.
        table = self._grid_row(panel, PANEL, CART_WEIGHTS, CART_MINSIZES, "cart")
        table.pack(fill="x", padx=14, pady=(3, 0))
        for column, title in enumerate(CART_TITLES):
            self._cell(table, column, title, header=True)
        # Stage 17: height=1. CTkFrame's 200px default made an EMPTY basket
        # occupy a fifth of the screen and forced the cashier to scroll.
        self.cart_frame = ctk.CTkFrame(
            panel, fg_color="transparent", height=1
        )
        self.cart_frame.pack(fill="x", padx=14)
        self.cart_note = ctk.CTkLabel(
            panel, text="", font=font(9), text_color=TEXT_MUTED,
            anchor="w", wraplength=480, justify="left", height=16,
        )
        self.cart_note.pack(fill="x", padx=16, pady=(1, 4))

    def _build_payment_panel(self, parent: Any) -> None:
        """Full-width checkout card.

        A side-by-side split squeezed this panel to 1px at 1160x720, because
        the basket table alone needs ~780px. Stacking the cards keeps TOTAL,
        the payment controls and Confirm sale visible without scrolling
        sideways (there is no horizontal scrollbar on the page).
        """
        panel = self._card(parent, "2  ·  Payment & checkout")
        panel.pack(fill="x", pady=(4, 0))

        body = ctk.CTkFrame(panel, fg_color="transparent")
        body.pack(fill="x", padx=14, pady=(4, 6))
        for column in range(3):
            body.grid_columnconfigure(column, weight=1, uniform="checkout")

        left = ctk.CTkFrame(body, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 18))
        middle = ctk.CTkFrame(body, fg_color="transparent")
        middle.grid(row=0, column=1, sticky="nsew", padx=(0, 18))
        right = ctk.CTkFrame(body, fg_color="transparent")
        right.grid(row=0, column=2, sticky="nsew")

        ctk.CTkLabel(
            left, text="TOTAL", font=font(10, "bold"),
            text_color=TEXT_MUTED, anchor="w", height=18,
        ).pack(fill="x")
        self.total_label = ctk.CTkLabel(
            left, text=format_money(0), font=font(24, "bold"),
            text_color=ACCENT, anchor="w",
        )
        self.total_label.pack(fill="x")
        self.item_count_label = ctk.CTkLabel(
            left, text="0 items", font=font(10), text_color=TEXT_MUTED,
            anchor="w", height=18,
        )
        self.item_count_label.pack(fill="x")
        self.shortage_label = ctk.CTkLabel(
            left, text="", font=font(10), text_color=DANGER,
            anchor="w", wraplength=200, justify="left", height=18,
        )
        self.shortage_label.pack(fill="x", pady=(3, 0))

        # Stage 16 - the answer the cashier actually needs. It is refreshed
        # by a StringVar write trace, so no button press is needed.
        ctk.CTkLabel(
            left, text="CHANGE", font=font(10, "bold"),
            text_color=TEXT_MUTED, anchor="w", height=18,
        ).pack(fill="x", pady=(2, 0))
        self.change_label = ctk.CTkLabel(
            left, text="", font=font(15, "bold"), text_color=TEXT_MUTED,
            anchor="w", wraplength=210, justify="left",
        )
        self.change_label.pack(fill="x")
        self.breakdown_label = ctk.CTkLabel(
            left, text="", font=font(9), text_color=TEXT_MUTED,
            anchor="w", wraplength=200, justify="left",
        )
        self.breakdown_label.pack(fill="x")

        ctk.CTkLabel(
            middle, text="Payment method", font=font(10, "bold"),
            text_color=TEXT, anchor="w", height=18,
        ).pack(fill="x")
        self.payment_combo = ctk.CTkComboBox(
            middle,
            values=list(products.PAYMENT_METHODS),
            height=30,
            corner_radius=10,
        )
        self.payment_combo.set("Cash")
        self.payment_combo.pack(fill="x")
        ctk.CTkLabel(
            middle,
            text=f"Amount tendered ({CURRENCY_SYMBOL})",
            font=font(10, "bold"), text_color=TEXT, anchor="w", height=18,
        ).pack(fill="x", pady=(3, 1))
        # Stage 16: NO placeholder. CTkEntry reports "" while a placeholder
        # is showing, which would make the live change jump around. The hint
        # lives in the label above instead. textvariable drives the trace.
        self.tendered_entry = ctk.CTkEntry(
            middle,
            textvariable=self.tendered_var,
            height=34,
            corner_radius=10,
            fg_color=PANEL,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT,
            font=font(15),
        )
        self.tendered_entry.pack(fill="x")
        ctk.CTkLabel(
            middle,
            text="Cash received - change is worked out automatically.",
            font=font(9), text_color=TEXT_MUTED, anchor="w", height=16,
        ).pack(fill="x")

        ctk.CTkLabel(
            middle, text="Quick cash - tap the bill you were handed",
            font=font(9, "bold"), text_color=TEXT_MUTED, anchor="w", height=16,
        ).pack(fill="x", pady=(2, 1))
        quick = ctk.CTkFrame(middle, fg_color="transparent")
        quick.pack(fill="x")
        for column in range(3):
            quick.grid_columnconfigure(column, weight=1, uniform="quickcash")
        for index, amount in enumerate(change.QUICK_CASH):
            ctk.CTkButton(
                quick,
                text=change.quick_cash_label(amount),
                command=lambda value=amount: self._quick_cash(value),
                height=24,
                corner_radius=8,
                fg_color=PANEL,
                hover_color="#F0E9DD",
                border_width=1,
                border_color=BORDER,
                text_color=TEXT,
                font=font(11, "bold"),
            ).grid(
                row=index // 3, column=index % 3,
                sticky="ew", padx=1, pady=0,
            )
        ctk.CTkButton(
            middle,
            text="Use exact total",
            command=self._use_exact_total,
            height=26,
            corner_radius=8,
            fg_color="transparent",
            hover_color=PANEL,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT_MUTED,
            font=font(11),
        ).pack(fill="x", pady=(2, 0))

        self.confirm_button = ctk.CTkButton(
            right,
            text="Confirm sale",
            command=self._confirm,
            height=40,
            corner_radius=10,
            fg_color=CONFIRM_DISABLED_COLOR,
            hover_color="#326349",
            text_color="#FFFFFF",
            font=font(15, "bold"),
        )
        self.confirm_button.pack(fill="x")
        ctk.CTkButton(
            right,
            text="Clear basket",
            command=self._clear,
            height=28,
            corner_radius=9,
            fg_color="transparent",
            hover_color=PANEL,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT_MUTED,
            font=font(11),
        ).pack(fill="x", pady=(5, 0))
        self.gross_label = ctk.CTkLabel(
            right, text="", font=font(9), text_color=TEXT_MUTED,
            anchor="w", wraplength=200, justify="left", height=16,
        )
        self.gross_label.pack(fill="x", pady=(5, 0))

        # Stage 17: the discount / calculator cards are NOT in this card any
        # more. They live in the collapsible "More tools" section built by
        # _build_tools(), so the checkout flow needs no scrolling at all.
        # Install LAST, after every widget exists: the first write must never
        # touch a half-built panel. This single trace covers typing, quick
        # cash, "Use exact total" and "Use result as amount tendered".
        self.tendered_var.trace_add(
            "write", lambda *_args: self._render_change()
        )
        self._render_change()

    def _build_tools(self) -> None:
        """Stage 17 - discount + calculator behind ONE labelled toggle.

        Stage 16 removed the mental arithmetic, so these are occasional tools
        rather than part of every sale. Nothing here is deleted: all widgets
        keep their names and stay reachable with a single click.
        """
        body = self._section(
            "tools",
            "▸  More tools: Senior/PWD discount + calculator",
            "▾  More tools: Senior/PWD discount + calculator",
        )
        extras = ctk.CTkFrame(body, fg_color="transparent", height=1)
        extras.pack(fill="x")
        extras.grid_columnconfigure(0, weight=1, uniform="stage15")
        extras.grid_columnconfigure(1, weight=1, uniform="stage15")

        disc = ctk.CTkFrame(
            extras, fg_color=CARD, corner_radius=12,
            border_width=1, border_color=BORDER,
        )
        disc.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        calc = ctk.CTkFrame(
            extras, fg_color=CARD, corner_radius=12,
            border_width=1, border_color=BORDER,
        )
        calc.grid(row=0, column=1, sticky="nsew")

        ctk.CTkLabel(
            disc, text="Senior Citizen / PWD discount",
            font=font(11, "bold"), text_color=TEXT, anchor="w",
        ).pack(fill="x", padx=12, pady=(8, 1))
        ctk.CTkLabel(
            disc, text="20% off the whole sale - one discount per transaction",
            font=font(9), text_color=TEXT_MUTED, anchor="w",
        ).pack(fill="x", padx=12, pady=(0, 5))
        self.discount_combo = ctk.CTkComboBox(
            disc, values=list(discounts.CHOICES), height=28,
            corner_radius=9, font=font(11),
            command=lambda _choice=None: self._render_cart(),
        )
        self.discount_combo.set(discounts.LABELS[discounts.NO_DISCOUNT])
        self.discount_combo.pack(fill="x", padx=12)

        fields = ctk.CTkFrame(disc, fg_color="transparent", height=1)
        fields.pack(fill="x", padx=12, pady=(6, 0))
        for column in range(2):
            fields.grid_columnconfigure(column, weight=1, uniform="discount")
        self.customer_name_entry = ctk.CTkEntry(
            fields, placeholder_text="Customer name", height=28,
            corner_radius=9, fg_color=PANEL, border_width=1,
            border_color=BORDER, text_color=TEXT, font=font(11),
        )
        self.customer_name_entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.customer_id_entry = ctk.CTkEntry(
            fields, placeholder_text="Senior / PWD ID no.", height=28,
            corner_radius=9, fg_color=PANEL, border_width=1,
            border_color=BORDER, text_color=TEXT, font=font(11),
        )
        self.customer_id_entry.grid(row=0, column=1, sticky="ew")
        for entry in (self.customer_name_entry, self.customer_id_entry):
            entry.bind("<KeyRelease>", lambda _event: self._render_cart())

        self.discount_status_label = ctk.CTkLabel(
            disc, text="", font=font(10, "bold"), text_color=TEXT_MUTED,
            anchor="w", wraplength=420, justify="left",
        )
        self.discount_status_label.pack(fill="x", padx=12, pady=(6, 2))
        ctk.CTkLabel(
            disc, text="Name and ID are required and appear on the receipt.",
            font=font(9), text_color=TEXT_MUTED, anchor="w",
        ).pack(fill="x", padx=12, pady=(0, 8))

        ctk.CTkLabel(
            calc, text="Cashier calculator",
            font=font(11, "bold"), text_color=TEXT, anchor="w",
        ).pack(fill="x", padx=12, pady=(8, 3))
        self.calc_display = ctk.CTkLabel(
            calc, text="0", font=font(16, "bold"), text_color=ACCENT,
            anchor="e", fg_color=PANEL, corner_radius=8, height=28,
        )
        self.calc_display.pack(fill="x", padx=12)
        pad = ctk.CTkFrame(calc, fg_color="transparent", height=1)
        pad.pack(fill="x", padx=12, pady=(5, 4))
        for column in range(4):
            pad.grid_columnconfigure(column, weight=1, uniform="calc")
        keypad = (
            ("C", "Del", "/", "x"),
            ("7", "8", "9", "-"),
            ("4", "5", "6", "+"),
            ("1", "2", "3", "="),
            ("00", "0", ".", "%"),
        )
        for row_index, row_keys in enumerate(keypad):
            for column_index, key in enumerate(row_keys):
                emphasise = key == "="
                ctk.CTkButton(
                    pad, text=key, height=24, corner_radius=7,
                    fg_color=ACCENT if emphasise else PANEL,
                    hover_color=ACCENT_HOVER if emphasise else "#F0E9DD",
                    border_width=0 if emphasise else 1,
                    border_color=BORDER,
                    text_color="#FFFFFF" if emphasise else TEXT,
                    font=font(11, "bold"),
                    command=lambda value=key: self._calc_press(value),
                ).grid(
                    row=row_index, column=column_index, sticky="ew",
                    padx=1, pady=1,
                )
        ctk.CTkButton(
            calc, text="Use result as amount tendered",
            command=self._use_calc_result, height=26, corner_radius=8,
            fg_color=PANEL, hover_color="#F0E9DD", border_width=1,
            border_color=BORDER, text_color=TEXT_MUTED, font=font(10),
        ).pack(fill="x", padx=12, pady=(2, 8))

    def _build_history(self) -> None:
        body = self._section(
            "history",
            "▸  Sales history - view / reprint / void",
            "▾  Sales history - view / reprint / void",
        )
        panel = self._card(body, "3  ·  Sales history")
        panel.pack(fill="x")
        tools = ctk.CTkFrame(panel, fg_color="transparent", height=1)
        tools.pack(fill="x", padx=14, pady=(6, 3))
        self.history_search = ctk.CTkEntry(
            tools,
            placeholder_text="Search transaction number, date or product...",
            height=28,
            corner_radius=9,
            fg_color=PANEL,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT,
            font=font(11),
            width=330,
        )
        self.history_search.pack(side="left")
        self.history_search.bind("<KeyRelease>", lambda _event: self._render_history())
        header = self._grid_row(
            panel, PANEL, HISTORY_WEIGHTS, HISTORY_MINSIZES, "history"
        )
        header.pack(fill="x", padx=14, pady=(2, 2))
        for column, title in enumerate(HISTORY_TITLES):
            self._cell(header, column, title, header=True)
        # Stage 17: height=1 - an empty history must not reserve 200px.
        self.history_frame = ctk.CTkFrame(
            panel, fg_color="transparent", height=1
        )
        self.history_frame.pack(fill="x", padx=14, pady=(0, 8))

    # ------------------------------------------------------------------
    # Data / rendering
    # ------------------------------------------------------------------
    def _message(self, text: str, *, error: bool = False) -> None:
        self.status_label.configure(
            text=text, text_color=DANGER if error else SUCCESS
        )

    def refresh(self) -> None:
        """Reload picker, re-price the basket and refresh Sales history."""
        try:
            self._reload_picker()
            self._render_cart()
            self._render_history()
            if self.is_manager:
                self.gross_label.configure(
                    text="Gross sales (completed): "
                         + format_money(store_sales.gross_sales())
                )
            else:
                self.gross_label.configure(text="")
        except (ValueError, sqlite3.Error, permissions.PermissionDenied) as exc:
            self._message(str(exc), error=True)

    def _reload_picker(self) -> None:
        try:
            rows = products.search_sellable(self.picker_search.get())
        except (ValueError, sqlite3.Error) as exc:
            self._message(str(exc), error=True)
            return
        self.products_by_label = {
            str(row["barcode"]) + "  |  " + str(row["name"]): str(row["barcode"])
            for row in rows
        }
        labels = list(self.products_by_label) or ["No products found"]
        self.picker_combo.configure(values=labels)
        self.picker_combo.set(labels[0])

    def _render_cart(self) -> None:
        for child in self.cart_frame.winfo_children():
            child.destroy()
        kind = self._discount_type()
        name = str(self.customer_name_entry.get()).strip()
        id_number = str(self.customer_id_entry.get()).strip()
        try:
            preview = (
                store_sales.preview_sale(self.cart, discount_type=kind)
                if self.cart else None
            )
        except (ValueError, sqlite3.Error) as exc:
            self._message(str(exc), error=True)
            preview = None
        # total_amount is the NET payable amount after the 20% discount.
        self.total_amount = float(preview["total_amount"]) if preview else 0.0
        details_ready = not kind or (len(name) >= 2 and len(id_number) >= 3)
        self.can_confirm = bool(
            preview and preview["can_confirm"] and details_ready
        )
        self._render_discount_status(kind, preview, details_ready)

        for index, item in enumerate(self.cart):
            row = self._grid_row(
                self.cart_frame, CARD, CART_WEIGHTS, CART_MINSIZES, "cart"
            )
            row.pack(fill="x", pady=1)
            self._cell(row, 0, str(item["name"]))
            self._cell(row, 1, str(item["barcode"]), color=TEXT_MUTED)
            self._cell(row, 2, str(item["quantity"]))
            self._cell(row, 3, format_money(item["unit_price"]))
            self._cell(row, 4, format_money(item["unit_price"] * item["quantity"]))
            actions = ctk.CTkFrame(row, fg_color="transparent")
            actions.grid(row=0, column=5, sticky="w", padx=3, pady=4)
            for label, delta in (("−", -1), ("+", 1)):
                ctk.CTkButton(
                    actions,
                    text=label,
                    command=lambda code=item["barcode"], step=delta: self._change_qty(code, step),
                    width=26,
                    height=24,
                    corner_radius=6,
                    fg_color=PANEL,
                    hover_color="#F0E9DD",
                    border_width=1,
                    border_color=BORDER,
                    text_color=TEXT,
                    font=font(11, "bold"),
                ).pack(side="left", padx=(0, 3))
            ctk.CTkButton(
                actions,
                text="Remove",
                command=lambda code=item["barcode"]: self._remove(code),
                width=64,
                height=24,
                corner_radius=6,
                fg_color=DANGER,
                hover_color="#8F2D21",
                text_color="#FFFFFF",
                font=font(10),
            ).pack(side="left")

        self.total_label.configure(text=format_money(self.total_amount))
        count = sum(int(item["quantity"]) for item in self.cart)
        self.item_count_label.configure(
            text=str(count) + " item(s) in " + str(len(self.cart)) + " line(s)"
        )
        shortages = list(preview["shortages"]) if preview else []
        # Stage 17: an empty shortage warning must NOT reserve a blank row.
        # It grows back as soon as a real stock problem needs to be shown.
        self.shortage_label.configure(
            text="; ".join(shortages), height=18 if shortages else 1
        )
        self.cart_note.configure(
            text="Basket is empty - scan a barcode to begin."
            if not self.cart else
            "Stock is checked again when you confirm. Nothing is deducted "
            "until then."
        )
        # Stage 16: scanning another item or applying a discount changes the
        # NET total, so the auto-calculated change must be refreshed too.
        # _render_change() also applies the confirm-button state.
        self._render_change()

    def _set_confirm_enabled(self, enabled: bool) -> None:
        colour = SUCCESS if enabled else CONFIRM_DISABLED_COLOR
        self.confirm_button.configure(
            state="normal" if enabled else "disabled", fg_color=colour
        )

    def _render_history(self) -> None:
        for child in self.history_frame.winfo_children():
            child.destroy()
        try:
            rows = store_sales.get_sales_history(self.history_search.get())
        except (ValueError, sqlite3.Error) as exc:
            self._message(str(exc), error=True)
            return
        for item in rows:
            row = self._grid_row(
                self.history_frame, CARD,
                HISTORY_WEIGHTS, HISTORY_MINSIZES, "history",
            )
            row.pack(fill="x", pady=1)
            self._cell(row, 0, str(item["transaction_number"]))
            self._cell(row, 1, str(item["transaction_date"]), color=TEXT_MUTED)
            self._cell(row, 2, str(item["item_count"]))
            self._cell(row, 3, format_money(item["total_amount"]))
            self._cell(row, 4, str(item["payment_method"]))
            voided = str(item["status"]) == "voided"
            self._cell(
                row, 5, "Voided" if voided else "Completed",
                color=DANGER if voided else SUCCESS,
            )
            actions = ctk.CTkFrame(row, fg_color="transparent")
            actions.grid(row=0, column=6, sticky="w", padx=4, pady=4)
            ctk.CTkButton(
                actions,
                text="View",
                command=lambda key=item["sale_id"]: self._view_sale(key),
                width=50, height=24, corner_radius=6,
                fg_color=PANEL, hover_color="#F0E9DD",
                border_width=1, border_color=BORDER,
                text_color=TEXT, font=font(10),
            ).pack(side="left", padx=(0, 4))
            ctk.CTkButton(
                actions,
                text="Reprint",
                command=lambda key=item["sale_id"]: self._reprint(key),
                width=64, height=24, corner_radius=6,
                fg_color=PANEL, hover_color="#F0E9DD",
                border_width=1, border_color=BORDER,
                text_color=TEXT, font=font(10),
            ).pack(side="left", padx=(0, 4))
            if self.can_void and not voided:
                ctk.CTkButton(
                    actions,
                    text="Void",
                    command=lambda key=item["sale_id"]: self._void(key),
                    width=48, height=24, corner_radius=6,
                    fg_color=DANGER, hover_color="#8F2D21",
                    text_color="#FFFFFF", font=font(10),
                ).pack(side="left")

    # ------------------------------------------------------------------
    # Basket actions
    # ------------------------------------------------------------------
    def _add_by_barcode(self, redirect: bool = False) -> None:
        code = self._raw_text(self.scan_entry).strip()
        # Clear the field FIRST, including on an error. A USB scanner types
        # straight into whatever is focused, so a rejected code left behind
        # would be concatenated with the next scan and ring up nothing.
        self.scan_entry.delete(0, "end")
        self.scan_entry.focus_set()
        if not code:
            self._message("Scan or type a barcode first.", error=True)
            return
        # Reject a partial / accidental scan before touching the database, so a
        # stray key or an incomplete barcode can never look up the wrong item.
        try:
            code = scanner.normalize_barcode(code)
        except ValueError as exc:
            self._message(str(exc), error=True)
            return
        # A bouncing scanner trigger can emit two Enters for one scan. Ignore a
        # duplicate that arrives inside the debounce window - but only on the
        # keyboard-wedge redirect path, so a deliberate repeat scan still adds.
        if redirect:
            now = time.monotonic() * 1000.0
            if not self.scan_debouncer.accept(code, now):
                return
        try:
            match = store_sales.lookup_product(code)
        except (ValueError, sqlite3.Error) as exc:
            self._message(str(exc), error=True)
            return
        self._add_product(match, code)

    def _add_selected(self) -> None:
        label = self.picker_combo.get()
        code = self.products_by_label.get(label)
        if not code:
            self._message("Choose a product from the list first.", error=True)
            return
        try:
            match = store_sales.lookup_product(code)
        except (ValueError, sqlite3.Error) as exc:
            self._message(str(exc), error=True)
            return
        self._add_product(match, code)

    def _add_product(self, match: dict[str, Any], code: str) -> None:
        on_hand = float(match["quantity_on_hand"])
        existing = next(
            (item for item in self.cart if item["barcode"] == code), None
        )
        if existing is not None:
            if existing["quantity"] + 1 > on_hand + 1e-9:
                self._message(
                    f"Only {format_number(on_hand)} {match['unit']} of "
                    f"{match['name']} are in stock.",
                    error=True,
                )
                return
            existing["quantity"] += 1
        else:
            if on_hand < 1:
                self._message(
                    f"{match['name']} is out of stock.", error=True
                )
                return
            self.cart.append(
                {
                    "barcode": str(match["barcode"]),
                    "name": str(match["name"]),
                    "unit": str(match["unit"]),
                    "unit_price": float(match["unit_price"]),
                    "on_hand": on_hand,
                    "quantity": 1,
                }
            )
        self._message(f"Added {match['name']} to the basket.")
        self._render_cart()

    def _change_qty(self, code: str, delta: int) -> None:
        for index, item in enumerate(self.cart):
            if item["barcode"] != code:
                continue
            new_quantity = item["quantity"] + delta
            if new_quantity < 1:
                self.cart.pop(index)
            elif new_quantity > item["on_hand"] + 1e-9:
                self._message(
                    f"Only {format_number(item['on_hand'])} {item['unit']} "
                    f"of {item['name']} are in stock.",
                    error=True,
                )
                return
            else:
                item["quantity"] = new_quantity
            break
        self._render_cart()

    def _remove(self, code: str) -> None:
        self.cart = [item for item in self.cart if item["barcode"] != code]
        self._message("")
        self._render_cart()

    def _clear(self) -> None:
        self.cart = []
        self.tendered_entry.delete(0, "end")
        self._reset_discount()
        self._message("Basket cleared - nothing was written to the database.")
        self._render_cart()

    def _use_exact_total(self) -> None:
        self.tendered_entry.delete(0, "end")
        self.tendered_entry.insert(0, format_number(self.total_amount))

    # ------------------------------------------------------------------
    # Stage 16 - automatic change ("auto deduct")
    # ------------------------------------------------------------------
    def _tendered_text(self) -> str:
        """Read the cash received from the ONE variable all inputs share."""
        try:
            return str(self.tendered_var.get())
        except Exception:  # widget torn down during a smoke / shutdown
            return str(self.tendered_entry.get())

    def _render_change(self) -> None:
        """Show the change live. Pure display - NEVER writes to the database."""
        if not (hasattr(self, "change_label") and hasattr(self, "confirm_button")):
            return
        info = change.compute(self.total_amount, self._tendered_text())
        if info["status"] in change.GOOD_STATUSES:
            colour = SUCCESS
        elif info["status"] in change.BAD_STATUSES:
            colour = DANGER
        else:
            colour = TEXT_MUTED
        self.change_label.configure(text=info["label"], text_color=colour)
        self.breakdown_label.configure(
            text=info["detail"], text_color=TEXT_MUTED
        )
        # Keep the Stage 12/15 meaning of can_confirm unchanged. A short
        # payment is shown in red here AND rejected by confirm_sale().
        self._set_confirm_enabled(self.can_confirm)

    def _quick_cash(self, amount: int) -> None:
        """One tap = the bill the customer handed over."""
        self.tendered_entry.delete(0, "end")
        self.tendered_entry.insert(0, format_number(amount))
        self._render_change()

    # ------------------------------------------------------------------
    # Stage 15 - discount selection and cashier calculator
    # ------------------------------------------------------------------
    def _discount_type(self) -> str:
        """Return '' | 'senior' | 'pwd' from the discount selector."""
        try:
            return discounts.normalize_discount_type(self.discount_combo.get())
        except (ValueError, AttributeError):
            return discounts.NO_DISCOUNT

    def _render_discount_status(
        self,
        kind: str,
        preview: dict[str, Any] | None,
        details_ready: bool,
    ) -> None:
        if not kind:
            self.discount_status_label.configure(
                text="No discount - 20% is available for Senior Citizen / PWD.",
                text_color=TEXT_MUTED,
            )
            return
        if not details_ready:
            self.discount_status_label.configure(
                text="Enter the customer name and ID number to apply the "
                     "20% discount.",
                text_color=DANGER,
            )
            return
        if not preview:
            self.discount_status_label.configure(
                text=f"{discounts.LABELS[kind]} 20% discount selected.",
                text_color=SUCCESS,
            )
            return
        self.discount_status_label.configure(
            text=(
                f"{discounts.LABELS[kind]} 20%: -"
                + format_money(preview["discount_amount"])
                + "  ·  Subtotal " + format_money(preview["subtotal_amount"])
                + "  ·  Net " + format_money(preview["total_amount"])
            ),
            text_color=SUCCESS,
        )

    def _reset_discount(self) -> None:
        """A discount must NEVER leak onto the next customer's sale."""
        self.discount_combo.set(discounts.LABELS[discounts.NO_DISCOUNT])
        self.customer_name_entry.delete(0, "end")
        self.customer_id_entry.delete(0, "end")

    def _calc_press(self, key: str) -> None:
        # calculator.Calculator owns EVERY keypad key (including "00"), so the
        # GUI never needs to special-case one of its own buttons.
        try:
            self.calc.press(key)
        except ValueError:
            return
        self.calc_display.configure(text=self.calc.display)

    def _use_calc_result(self) -> None:
        amount = self.calc.value
        if self.calc.error or amount <= 0:
            self._message("The calculator result is not a valid amount.", error=True)
            return
        self.tendered_entry.delete(0, "end")
        self.tendered_entry.insert(0, format_number(amount))
        self._message(f"Amount tendered set to {format_money(amount)}.")

    # ------------------------------------------------------------------
    # Confirm / receipts / void
    # ------------------------------------------------------------------
    def _confirm(self) -> None:
        # A double click on Confirm must not create two sales or two receipts.
        if self._confirming:
            return
        if not self.can_confirm or not self.cart:
            self._message("The basket has a problem - check the stock warning.", error=True)
            return
        self._confirming = True
        try:
            cart = [
                {"barcode": item["barcode"], "quantity": item["quantity"]}
                for item in self.cart
            ]
            try:
                result = store_sales.confirm_sale(
                    cart,
                    self.payment_combo.get(),
                    self.tendered_entry.get(),
                    user_id=self.user.get("user_id"),
                    actor=self.actor,
                    discount_type=self._discount_type(),
                    customer_name=self.customer_name_entry.get(),
                    customer_id=self.customer_id_entry.get(),
                )
            except (ValueError, sqlite3.Error, permissions.PermissionDenied) as exc:
                self._message(str(exc), error=True)
                self._render_cart()
                return

            # Receipt is built, archived and printed AFTER the sale has
            # committed, inside ONE SQLite transaction started by
            # store_sales.confirm_sale(). A printer / disk failure here can
            # only affect printing - it can never reverse a paid sale.
            try:
                outcome = receipt.issue_store_receipt(
                    result, auto_print=self.auto_print, base_dir=self.receipt_dir
                )
                detail = outcome["print"]["detail"]
                self._message(
                    f"Sale {result['transaction_number']} confirmed. "
                    f"Total {format_money(result['total_amount'])}. {detail}"
                )
            except OSError as exc:
                self._message(
                    f"Sale {result['transaction_number']} confirmed, but the "
                    f"receipt could not be archived: {exc}. The sale stands.",
                    error=True,
                )
            self.cart = []
            self.tendered_entry.delete(0, "end")
            # Never let a Senior/PWD discount or a calculator result carry
            # over to the next customer.
            self._reset_discount()
            self.calc.clear()
            self.calc_display.configure(text=self.calc.display)
            self.scan_entry.focus_set()
            self.refresh()
        finally:
            self._confirming = False

    def _sale_record(self, sale_id: Any) -> dict[str, Any]:
        sale = store_sales.get_sale_detail(sale_id)
        sale["items"] = list(sale.get("items") or [])
        return sale

    def _view_sale(self, sale_id: Any) -> None:
        try:
            sale = self._sale_record(sale_id)
        except (ValueError, sqlite3.Error) as exc:
            self._message(str(exc), error=True)
            return
        self.dialog = ReceiptDialog(
            self, receipt.build_store_receipt(sale, reprint=True)
        )

    def _reprint(self, sale_id: Any) -> None:
        try:
            sale = self._sale_record(sale_id)
            outcome = receipt.issue_store_receipt(
                sale,
                reprint=True,
                auto_print=self.auto_print,
                base_dir=self.receipt_dir,
            )
        except (ValueError, sqlite3.Error, OSError) as exc:
            self._message(str(exc), error=True)
            return
        self._message("Receipt reprinted. " + outcome["print"]["detail"])

    def _void(self, sale_id: Any) -> None:
        if not permissions.can(self.user, permissions.CAP_VOID_SALE):
            self._message(
                "Only a manager can void a sale.", error=True
            )
            return
        self.dialog = VoidDialog(
            self, lambda reason: self._do_void(sale_id, reason)
        )

    def _do_void(self, sale_id: Any, reason: str) -> None:
        try:
            voided = store_sales.void_sale(
                sale_id, reason,
                user_id=self.user.get("user_id"),
                actor=self.actor,
            )
            receipt.issue_store_receipt(
                voided, auto_print=False, base_dir=self.receipt_dir
            )
        except (ValueError, sqlite3.Error, permissions.PermissionDenied) as exc:
            self._message(str(exc), error=True)
            return
        self._message(
            f"Sale #{sale_id} was voided. Products were returned to stock."
        )
        self.refresh()

    # ------------------------------------------------------------------
    # Stage 14 - physical USB HID barcode scanner + receipt printer
    # ------------------------------------------------------------------
    def _open_printer_dialog(self) -> None:
        """Local device setting, available to both roles.

        It only writes config/printer.json; no business capability is needed.
        The dialog enumerates installed Windows printers rather than guessing
        a device name, so any ESC/POS USB printer works.
        """
        if self._dialog_active():
            return
        self.dialog = PrinterDialog(self, on_status=self._message)

    def _install_scanner_focus(self) -> None:
        """Make a keyboard-wedge scanner work without repeated clicking.

        Every widget's bindtags include this window's toplevel, so ONE
        <Key> binding there observes all keystrokes. scanner.ScanBuffer
        separates a fast digit burst (a scanner) from normal human typing.
        If Enter completes a burst that landed in the WRONG field, we remove
        those stray digits, move them into the scan field and ring them up.
        A low-frequency guard returns the caret to the scan field when the
        counter is idle and focus has drifted to a button.
        """
        try:
            self._scanner_root = self.winfo_toplevel()
        except Exception:
            self._scanner_root = None
        if self._scanner_root is None:
            return
        try:
            self._key_funcid = self._scanner_root.bind(
                "<Key>", self._on_root_key, add="+"
            )
            # AppShell destroys (not hides) a page on navigation, so this is
            # the reliable place to release the toplevel binding and the
            # after() loop. Otherwise stale callbacks outlive the page.
            self._destroy_funcid = self.bind(
                "<Destroy>", self._teardown_scanner_focus, add="+"
            )
        except Exception:
            self._key_funcid = None
            self._destroy_funcid = None
            return
        self._last_human_key_ms = time.monotonic() * 1000.0
        try:
            self.scan_entry.focus_set()
            self._guard_after = self.after(FOCUS_GUARD_MS, self._focus_guard)
        except Exception:
            self._guard_after = None

    def _teardown_scanner_focus(self, _event: Any = None) -> None:
        """Release the root binding and the after() loop when the page dies."""
        if self._guard_after is not None:
            try:
                self.after_cancel(self._guard_after)
            except Exception:
                pass
            self._guard_after = None
        if self._key_funcid is not None and self._scanner_root is not None:
            try:
                self._scanner_root.unbind("<Key>", self._key_funcid)
            except Exception:
                pass
        self._key_funcid = None
        self._destroy_funcid = None
        self.scan_buffer.reset()
        self.scan_debouncer.reset()
        self._burst_widget = None

    def _dialog_active(self) -> bool:
        dialog = self.dialog
        if dialog is None:
            return False
        try:
            return bool(dialog.winfo_exists())
        except Exception:
            self.dialog = None
            return False

    def _is_scan_focus(self, widget: Any) -> bool:
        """CTkEntry delegates focus to its inner tkinter.Entry."""
        if widget is None:
            return False
        inner = getattr(self.scan_entry, "_entry", None)
        return widget is self.scan_entry or (
            inner is not None and widget is inner
        )

    @staticmethod
    def _is_text_widget(widget: Any) -> bool:
        """True for an entry/text widget the cashier may be typing into."""
        if widget is None:
            return False
        return all(
            hasattr(widget, name) for name in ("get", "insert", "delete")
        )

    @staticmethod
    def _raw_text(widget: Any) -> str:
        """Read a CTkEntry's REAL content, ignoring placeholder bookkeeping.

        CTkEntry.get() deliberately returns "" while its placeholder is
        active, even when characters have already been inserted. A HID
        scanner can hit exactly that window when the POS window regains
        focus. Scanner input must therefore read the underlying
        tkinter.Entry (the barcode field itself no longer has a placeholder,
        but other fields still do).
        """
        inner = getattr(widget, "_entry", None)
        try:
            return str(inner.get() if inner is not None else widget.get())
        except Exception:
            return ""

    @staticmethod
    def _strip_burst_tail(widget: Any, code: str) -> None:
        """Remove digits a scanner typed into the wrong field."""
        if widget is None or not code:
            return
        inner = getattr(widget, "_entry", widget)
        if not all(hasattr(inner, name) for name in ("get", "delete")):
            return
        try:
            text = str(inner.get())
            tail = len(code)
            if len(text) >= tail and text[-tail:] == code:
                inner.delete(len(text) - tail, "end")
        except Exception:
            pass

    def _on_root_key(self, event: Any) -> str | None:
        """Observe every keystroke; never interfere with normal typing.

        Only a fast digit burst is treated as a scanner. A modal dialog or
        an in-flight Confirm disables the redirect entirely, so a scan can
        never be mistaken for a void reason or a second sale.
        """
        if self._dialog_active() or self._confirming:
            self.scan_buffer.reset()
            self._burst_widget = None
            return None
        now = time.monotonic() * 1000.0
        widget = getattr(event, "widget", None)
        char = str(getattr(event, "char", "") or "")
        keysym = str(getattr(event, "keysym", "") or "")

        if keysym in ("Return", "KP_Enter") or char in ("\r", "\n"):
            return self._on_burst_enter(widget)

        self._last_human_key_ms = now
        if (
            not char
            or not char.isprintable()
            or not scanner.is_barcode_char(char)
        ):
            self.scan_buffer.reset()
            self._burst_widget = None
            return None
        # Focus changed in the middle of a code: treat it as human typing.
        if self.scan_buffer.chars and widget is not self._burst_widget:
            self.scan_buffer.reset()
            self._burst_widget = None
        self.scan_buffer.feed(char, now)
        self._burst_widget = widget if self.scan_buffer.chars else None
        return None

    def _on_burst_enter(self, widget: Any) -> str | None:
        """A scanner's Enter suffix completes a burst. Ring up exactly once."""
        if not self.scan_buffer.active:
            self.scan_buffer.reset()
            self._burst_widget = None
            return None
        code = self.scan_buffer.finish()  # also resets the buffer
        source = self._burst_widget or widget
        self._burst_widget = None
        if not code:
            return None
        if self._is_scan_focus(widget):
            # The scan field's own <Return> binding already added the item.
            return None
        if self._is_scan_focus(source):
            # Digits are in the scan field but Enter arrived elsewhere.
            self._add_by_barcode(redirect=True)
            return "break"
        # The digits landed in another widget: undo them there, then add the
        # product from the scan field so the normal lookup path is reused.
        self._strip_burst_tail(source, code)
        self.scan_entry.delete(0, "end")
        self.scan_entry.insert(0, code)
        self.scan_entry.focus_set()
        self._add_by_barcode(redirect=True)
        return "break"

    def _focus_guard(self) -> None:
        """Return the caret to the scan field while the counter is idle.

        Deliberately conservative: it never steals focus from a text field
        the cashier is using, from a modal dialog, or during Confirm. The
        root <Key> redirect already handles scans from ANY focused widget,
        so this is only a convenience for the next scan.
        """
        self._guard_after = None
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        try:
            now = time.monotonic() * 1000.0
            idle = now - self._last_human_key_ms >= IDLE_REFOCUS_MS
            if idle and not self._dialog_active() and not self._confirming:
                focused = self.focus_get()
                if (
                    not self._is_scan_focus(focused)
                    and not self._is_text_widget(focused)
                ):
                    self.scan_entry.focus_set()
        except Exception:
            pass
        try:
            self._guard_after = self.after(FOCUS_GUARD_MS, self._focus_guard)
        except Exception:
            self._guard_after = None

    # ------------------------------------------------------------------
    # Shared row helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _grid_row(
        parent: Any,
        background: str,
        weights: tuple[int, ...],
        minsizes: tuple[int, ...],
        group: str,
    ) -> ctk.CTkFrame:
        row = ctk.CTkFrame(parent, fg_color=background, corner_radius=8)
        for index, weight in enumerate(weights):
            row.grid_columnconfigure(
                index, weight=weight, minsize=minsizes[index], uniform=group
            )
        return row

    @staticmethod
    def _cell(
        row: Any, column: int, text: str, *,
        header: bool = False, color: str | None = None,
    ) -> None:
        ctk.CTkLabel(
            row,
            text=text,
            font=font(10, "bold") if header else font(10),
            text_color=color or (TEXT_MUTED if header else TEXT),
            anchor="w",
            wraplength=175,
            justify="left",
            # Stage 17: CTkLabel defaults to 28px. Shorter cells keep a full
            # basket on screen without shrinking the numbers or buttons.
            height=22,
        ).grid(row=0, column=column, sticky="w", padx=6, pady=4)
