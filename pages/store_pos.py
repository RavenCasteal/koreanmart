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
    3. Choose the payment method, type the amount tendered (or press Exact).
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
"""

from __future__ import annotations

import sqlite3
from tkinter import messagebox
from typing import Any, Callable

import customtkinter as ctk

import permissions
import products
import receipt
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
        # Tests / smoke runs turn printing off; the receipt is still archived.
        self.auto_print = True
        self.receipt_dir: Any = None

        self.can_void = permissions.can(self.user, permissions.CAP_VOID_SALE)
        self.is_manager = permissions.is_manager(self.user)

        self.scroller = ctk.CTkScrollableFrame(
            self, fg_color=CREAM, corner_radius=0
        )
        self.scroller.pack(fill="both", expand=True)
        self.container = ctk.CTkFrame(self.scroller, fg_color="transparent")
        self.container.pack(fill="both", expand=True, padx=26, pady=20)

        self._build_header()
        split = ctk.CTkFrame(self.container, fg_color="transparent")
        split.pack(fill="x")
        self._build_scan_panel(split)
        self._build_payment_panel(split)
        self._build_history()
        self.refresh()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _build_header(self) -> None:
        head = ctk.CTkFrame(self.container, fg_color="transparent")
        head.pack(fill="x", pady=(0, 12))
        ctk.CTkLabel(
            head, text="Point of Sale", font=font(20, "bold"), text_color=TEXT
        ).pack(side="left")
        ctk.CTkLabel(
            head,
            text="Scan a barcode, build the basket, then confirm  ·  PHP / "
                 + CURRENCY_SYMBOL,
            font=font(11), text_color=TEXT_MUTED,
        ).pack(side="right")
        self.status_label = ctk.CTkLabel(
            self.container,
            text="",
            font=font(12),
            text_color=SUCCESS,
            anchor="w",
            wraplength=840,
            justify="left",
            height=24,
        )
        self.status_label.pack(fill="x", pady=(0, 8))

    def _card(self, parent: Any, title: str) -> ctk.CTkFrame:
        panel = ctk.CTkFrame(
            parent, fg_color=CARD, corner_radius=14,
            border_width=1, border_color=BORDER,
        )
        ctk.CTkLabel(
            panel, text=title, font=font(15, "bold"), text_color=TEXT,
            anchor="w",
        ).pack(fill="x", padx=18, pady=(16, 0))
        return panel

    def _build_scan_panel(self, parent: Any) -> None:
        panel = self._card(parent, "1  ·  Scan products")
        panel.pack(fill="x")

        ctk.CTkLabel(
            panel,
            text="Barcode scanner / manual entry",
            font=font(11, "bold"), text_color=TEXT_MUTED, anchor="w",
        ).pack(fill="x", padx=18, pady=(12, 4))
        scan_row = ctk.CTkFrame(panel, fg_color="transparent")
        scan_row.pack(fill="x", padx=18)
        self.scan_entry = ctk.CTkEntry(
            scan_row,
            placeholder_text="Scan barcode and press Enter...",
            height=44,
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
            height=44,
            corner_radius=10,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            text_color="#FFFFFF",
            font=font(13, "bold"),
        ).pack(side="left")

        ctk.CTkLabel(
            panel,
            text="Damaged barcode? Search by name instead",
            font=font(11), text_color=TEXT_MUTED, anchor="w",
        ).pack(fill="x", padx=18, pady=(14, 4))
        pick_row = ctk.CTkFrame(panel, fg_color="transparent")
        pick_row.pack(fill="x", padx=18)
        self.picker_search = ctk.CTkEntry(
            pick_row,
            placeholder_text="Type a product name...",
            height=34,
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
            height=34,
            corner_radius=9,
        )
        self.picker_combo.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkButton(
            pick_row,
            text="Add",
            command=self._add_selected,
            width=64,
            height=34,
            corner_radius=9,
            fg_color=PANEL,
            hover_color="#F0E9DD",
            border_width=1,
            border_color=BORDER,
            text_color=TEXT,
            font=font(11),
        ).pack(side="left")

        ctk.CTkLabel(
            panel,
            text="Basket",
            font=font(12, "bold"), text_color=TEXT, anchor="w",
        ).pack(fill="x", padx=18, pady=(16, 6))
        table = self._grid_row(panel, PANEL, CART_WEIGHTS, CART_MINSIZES, "cart")
        table.pack(fill="x", padx=18)
        for column, title in enumerate(CART_TITLES):
            self._cell(table, column, title, header=True)
        self.cart_frame = ctk.CTkFrame(panel, fg_color="transparent")
        self.cart_frame.pack(fill="x", padx=18)
        self.cart_note = ctk.CTkLabel(
            panel, text="", font=font(11), text_color=TEXT_MUTED,
            anchor="w", wraplength=480, justify="left",
        )
        self.cart_note.pack(fill="x", padx=20, pady=(6, 16))

    def _build_payment_panel(self, parent: Any) -> None:
        """Full-width checkout card.

        A side-by-side split squeezed this panel to 1px at 1160x720, because
        the basket table alone needs ~780px. Stacking the cards keeps TOTAL,
        the payment controls and Confirm sale visible without scrolling
        sideways (there is no horizontal scrollbar on the page).
        """
        panel = self._card(parent, "2  ·  Payment & checkout")
        panel.pack(fill="x", pady=(14, 0))

        body = ctk.CTkFrame(panel, fg_color="transparent")
        body.pack(fill="x", padx=18, pady=(12, 18))
        for column in range(3):
            body.grid_columnconfigure(column, weight=1, uniform="checkout")

        left = ctk.CTkFrame(body, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 18))
        middle = ctk.CTkFrame(body, fg_color="transparent")
        middle.grid(row=0, column=1, sticky="nsew", padx=(0, 18))
        right = ctk.CTkFrame(body, fg_color="transparent")
        right.grid(row=0, column=2, sticky="nsew")

        ctk.CTkLabel(
            left, text="TOTAL", font=font(12, "bold"),
            text_color=TEXT_MUTED, anchor="w",
        ).pack(fill="x")
        self.total_label = ctk.CTkLabel(
            left, text=format_money(0), font=font(30, "bold"),
            text_color=ACCENT, anchor="w",
        )
        self.total_label.pack(fill="x")
        self.item_count_label = ctk.CTkLabel(
            left, text="0 items", font=font(11), text_color=TEXT_MUTED,
            anchor="w",
        )
        self.item_count_label.pack(fill="x")
        self.shortage_label = ctk.CTkLabel(
            left, text="", font=font(11), text_color=DANGER,
            anchor="w", wraplength=230, justify="left", height=34,
        )
        self.shortage_label.pack(fill="x", pady=(8, 0))

        ctk.CTkLabel(
            middle, text="Payment method", font=font(11, "bold"),
            text_color=TEXT, anchor="w",
        ).pack(fill="x", pady=(0, 4))
        self.payment_combo = ctk.CTkComboBox(
            middle,
            values=list(products.PAYMENT_METHODS),
            height=36,
            corner_radius=10,
        )
        self.payment_combo.set("Cash")
        self.payment_combo.pack(fill="x")
        ctk.CTkLabel(
            middle,
            text=f"Amount tendered ({CURRENCY_SYMBOL})",
            font=font(11, "bold"), text_color=TEXT, anchor="w",
        ).pack(fill="x", pady=(12, 4))
        self.tendered_entry = ctk.CTkEntry(
            middle,
            placeholder_text="0.00",
            height=40,
            corner_radius=10,
            fg_color=PANEL,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT,
            font=font(15),
        )
        self.tendered_entry.pack(fill="x")
        ctk.CTkButton(
            middle,
            text="Use exact total",
            command=self._use_exact_total,
            height=30,
            corner_radius=8,
            fg_color="transparent",
            hover_color=PANEL,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT_MUTED,
            font=font(11),
        ).pack(fill="x", pady=(6, 0))

        self.confirm_button = ctk.CTkButton(
            right,
            text="Confirm sale",
            command=self._confirm,
            height=48,
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
            height=34,
            corner_radius=9,
            fg_color="transparent",
            hover_color=PANEL,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT_MUTED,
            font=font(12),
        ).pack(fill="x", pady=(10, 0))
        self.gross_label = ctk.CTkLabel(
            right, text="", font=font(11), text_color=TEXT_MUTED,
            anchor="w", wraplength=230, justify="left",
        )
        self.gross_label.pack(fill="x", pady=(12, 0))

    def _build_history(self) -> None:
        panel = self._card(self.container, "3  ·  Sales history")
        panel.pack(fill="x", pady=(16, 0))
        tools = ctk.CTkFrame(panel, fg_color="transparent")
        tools.pack(fill="x", padx=18, pady=(10, 6))
        self.history_search = ctk.CTkEntry(
            tools,
            placeholder_text="Search transaction number, date or product...",
            height=32,
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
        header.pack(fill="x", padx=18, pady=(4, 4))
        for column, title in enumerate(HISTORY_TITLES):
            self._cell(header, column, title, header=True)
        self.history_frame = ctk.CTkFrame(panel, fg_color="transparent")
        self.history_frame.pack(fill="x", padx=18, pady=(0, 16))

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
        try:
            preview = store_sales.preview_sale(self.cart) if self.cart else None
        except (ValueError, sqlite3.Error) as exc:
            self._message(str(exc), error=True)
            preview = None
        self.total_amount = float(preview["total_amount"]) if preview else 0.0
        self.can_confirm = bool(preview and preview["can_confirm"])

        for index, item in enumerate(self.cart):
            row = self._grid_row(
                self.cart_frame, CARD, CART_WEIGHTS, CART_MINSIZES, "cart"
            )
            row.pack(fill="x", pady=2)
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
        self.shortage_label.configure(text="; ".join(shortages))
        self.cart_note.configure(
            text="Basket is empty - scan a barcode to begin."
            if not self.cart else
            "Stock is checked again when you confirm. Nothing is deducted "
            "until then."
        )
        self._set_confirm_enabled(self.can_confirm)

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
            row.pack(fill="x", pady=2)
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
    def _add_by_barcode(self) -> None:
        code = self.scan_entry.get().strip()
        # Clear the field FIRST, including on an error. A USB scanner types
        # straight into whatever is focused, so a rejected code left behind
        # would be concatenated with the next scan and ring up nothing.
        self.scan_entry.delete(0, "end")
        self.scan_entry.focus_set()
        if not code:
            self._message("Scan or type a barcode first.", error=True)
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
        self._message("Basket cleared - nothing was written to the database.")
        self._render_cart()

    def _use_exact_total(self) -> None:
        self.tendered_entry.delete(0, "end")
        self.tendered_entry.insert(0, format_number(self.total_amount))

    # ------------------------------------------------------------------
    # Confirm / receipts / void
    # ------------------------------------------------------------------
    def _confirm(self) -> None:
        if not self.can_confirm or not self.cart:
            self._message("The basket has a problem - check the stock warning.", error=True)
            return
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
            )
        except (ValueError, sqlite3.Error, permissions.PermissionDenied) as exc:
            self._message(str(exc), error=True)
            self._render_cart()
            return

        # Receipt is built, archived and printed AFTER the sale has committed.
        outcome = receipt.issue_store_receipt(
            result, auto_print=self.auto_print, base_dir=self.receipt_dir
        )
        detail = outcome["print"]["detail"]
        self._message(
            f"Sale {result['transaction_number']} confirmed. "
            f"Total {format_money(result['total_amount'])}. {detail}"
        )
        self.cart = []
        self.tendered_entry.delete(0, "end")
        self.scan_entry.focus_set()
        self.refresh()

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
            font=font(11, "bold") if header else font(11),
            text_color=color or (TEXT_MUTED if header else TEXT),
            anchor="w",
            wraplength=175,
            justify="left",
        ).grid(row=0, column=column, sticky="w", padx=6, pady=7)
