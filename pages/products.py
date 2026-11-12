"""
Stage 12 - Products / barcode catalog screen (MANAGER ONLY).

This is the convenience-store replacement for the old single-dish menu. A
manager adds packaged foods, drinks, sweets and ready-to-eat meals, each with
a barcode, a category, a Philippine-peso price, a minimum stock level and an
optional expiration date. A USB scanner simply types the barcode into the
Point of Sale screen, so no extra library is needed.

Rules:
  * A product IS a stock item. Physical stock is never edited from this form:
    use the Inventory screen for stock-in / stock-out. Opening stock is
    recorded as a real stock-in movement by the data layer.
  * Deactivation is a status change, never a DELETE, so old receipts and
    movement history keep valid references.
  * Editing a price or barcode never rewrites a past sale (receipts use
    snapshots).
  * All SQL lives in products.py. This file only builds the interface.
"""

from __future__ import annotations

import sqlite3
from tkinter import messagebox
from typing import Any, Callable

import customtkinter as ctk

import permissions
import products
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

# grid "uniform" scales every column by the largest minsize/weight ratio.
# weight ~= minsize/10 keeps all 8 columns inside the 1160x720 window, so
# "Edit" and "Deactivate" are both visible without a horizontal scrollbar.
TABLE_WEIGHTS = (14, 11, 9, 7, 7, 5, 7, 16)
TABLE_MINSIZES = (140, 110, 90, 70, 70, 50, 70, 160)
TABLE_TITLES = (
    "Product", "Barcode", "Category", "Price", "On hand",
    "Min", "Expiry", "Actions",
)


class ProductDialog(ModalDialog):
    """Add or edit one product. Prices are always Philippine peso."""

    def __init__(
        self,
        master: Any,
        actor: Any,
        on_saved: Callable[[str], None],
        target: dict[str, Any] | None = None,
    ) -> None:
        editing = target is not None
        super().__init__(master, "Edit product" if editing else "Add product", 500)
        self.actor = actor
        self.target = target
        self.on_saved = on_saved

        self.heading(
            "Edit product" if editing else "Add a product",
            "Scan-to-sell item. The barcode must be 6-14 digits. Prices are "
            "in Philippine peso (PHP). Stock is changed on the Inventory screen.",
        )
        self.name = self.field("Product name", on_enter=self._save)
        self.barcode = self.field("Barcode (numbers only)", on_enter=self._save)
        self.category = self.field("Category", on_enter=self._save)
        self.unit = self.field("Unit (piece, pack, bottle, ...)", on_enter=self._save)
        self.price = self.field(
            f"Selling price ({CURRENCY_SYMBOL})", on_enter=self._save
        )
        self.minimum = self.field("Minimum stock", on_enter=self._save)
        self.expiry = self.field("Expiration date (YYYY-MM-DD, optional)", on_enter=self._save)
        self.opening = self.field("Opening stock", on_enter=self._save)

        if editing:
            self.name.insert(0, str(target["name"]))
            self.barcode.insert(0, str(target["barcode"]))
            self.category.insert(0, str(target["category"]))
            self.unit.insert(0, str(target["unit"]))
            self.price.insert(0, format_number(target["unit_price"]))
            self.minimum.insert(0, format_number(target["minimum_stock"]))
            self.expiry.insert(0, str(target["expiration_date"] or ""))
            self.opening.insert(0, "Use the Inventory screen")
            self.opening.configure(state="disabled")
        else:
            self.category.insert(0, "Food")
            self.unit.insert(0, "piece")
            self.price.insert(0, "0.00")
            self.minimum.insert(0, "0")
            self.opening.insert(0, "0")

        self.category_menu = ctk.CTkOptionMenu(
            self.card,
            values=list(products.CATEGORIES),
            command=self._choose_category,
            height=34,
            corner_radius=10,
            fg_color=PANEL,
            button_color=BORDER,
            text_color=TEXT,
            font=font(12),
        )
        self.category_menu.set(self.category.get() or "Food")
        self.category_menu.pack(fill="x", padx=24, pady=(4, 0))

        self.error_label(height=44)
        self.actions("Save product" if editing else "Add product", self._save)
        self.finish(self.name)

    def _choose_category(self, choice: str) -> None:
        self.category.delete(0, "end")
        self.category.insert(0, choice)

    def _save(self) -> None:
        self.show_error("")
        try:
            if self.target is None:
                product_id = products.add_product(
                    self.name.get(),
                    self.barcode.get(),
                    self.category.get(),
                    self.unit.get(),
                    self.price.get(),
                    self.minimum.get(),
                    self.expiry.get(),
                    self.opening.get(),
                    actor=self.actor,
                )
                message = (
                    f"Product #{product_id} added. Scan its barcode on the "
                    "Point of Sale screen to sell it."
                )
            else:
                result = products.update_product(
                    self.target["ingredient_id"],
                    self.name.get(),
                    self.barcode.get(),
                    self.category.get(),
                    self.unit.get(),
                    self.price.get(),
                    self.minimum.get(),
                    self.expiry.get(),
                    actor=self.actor,
                )
                message = f"'{result['name']}' was updated."
        except (ValueError, sqlite3.Error, permissions.PermissionDenied) as exc:
            self.show_error(str(exc))
            return
        self.close()
        self.on_saved(message)


class ProductsView(ctk.CTkFrame):
    """Manager-only product catalog with search, add / edit / deactivate."""

    def __init__(
        self,
        master: Any,
        user: dict[str, Any] | None = None,
        on_navigate: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(master, fg_color=CREAM, corner_radius=0)
        self.actor = user or {}
        self.on_navigate = on_navigate
        self.dialog: ModalDialog | None = None
        self.allowed = permissions.can(
            self.actor, permissions.CAP_MANAGE_PRODUCTS
        )

        self.scroller = ctk.CTkScrollableFrame(
            self, fg_color=CREAM, corner_radius=0
        )
        self.scroller.pack(fill="both", expand=True)
        self.container = ctk.CTkFrame(self.scroller, fg_color="transparent")
        self.container.pack(fill="both", expand=True, padx=26, pady=22)

        if not self.allowed:
            self._build_denied()
            return

        self._build_toolbar()
        self.summary_frame = ctk.CTkFrame(self.container, fg_color="transparent")
        self.summary_frame.pack(fill="x", pady=(0, 14))
        self._build_table()
        self._build_notes()
        self.refresh()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _build_denied(self) -> None:
        ctk.CTkLabel(
            self.container,
            text="Manager access required",
            font=font(20, "bold"),
            text_color=TEXT,
            anchor="w",
        ).pack(anchor="w", pady=(8, 6))
        ctk.CTkLabel(
            self.container,
            text="Product and barcode management is visible only to a "
                 "manager. Please sign in with a manager account.",
            font=font(12),
            text_color=TEXT_MUTED,
            anchor="w",
            wraplength=760,
            justify="left",
        ).pack(anchor="w")

    def _build_toolbar(self) -> None:
        top = ctk.CTkFrame(self.container, fg_color="transparent")
        top.pack(fill="x", pady=(0, 10))
        left = ctk.CTkFrame(top, fg_color="transparent")
        left.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(
            left, text="Products & barcodes", font=font(20, "bold"),
            text_color=TEXT,
        ).pack(anchor="w")
        ctk.CTkLabel(
            left,
            text="Manager only  ·  scan-to-sell catalog  ·  prices in PHP",
            font=font(11), text_color=TEXT_MUTED, anchor="w",
        ).pack(anchor="w")

        buttons = ctk.CTkFrame(top, fg_color="transparent")
        buttons.pack(side="right")
        ctk.CTkButton(
            buttons,
            text="+  Add product",
            command=self._add_product,
            width=140,
            height=34,
            corner_radius=8,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            text_color="#FFFFFF",
            font=font(12, "bold"),
        ).pack(side="left")

        filters = ctk.CTkFrame(self.container, fg_color="transparent")
        filters.pack(fill="x", pady=(0, 12))
        self.search_entry = ctk.CTkEntry(
            filters,
            placeholder_text="Search by product name or barcode...",
            height=36,
            corner_radius=10,
            fg_color=PANEL,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT,
            font=font(12),
            width=300,
        )
        self.search_entry.pack(side="left", padx=(0, 10))
        self.search_entry.bind("<KeyRelease>", lambda _event: self.refresh())
        self.category_menu = ctk.CTkOptionMenu(
            filters,
            values=["All"] + list(products.CATEGORIES),
            command=lambda _choice: self.refresh(),
            width=170,
            height=36,
            corner_radius=10,
            fg_color=PANEL,
            button_color=BORDER,
            text_color=TEXT,
            font=font(12),
        )
        self.category_menu.set("All")
        self.category_menu.pack(side="left")

        self.status_label = ctk.CTkLabel(
            self.container,
            text="",
            font=font(12),
            text_color=SUCCESS,
            anchor="w",
            wraplength=820,
            justify="left",
            height=22,
        )
        self.status_label.pack(fill="x", pady=(0, 4))

    def _build_table(self) -> None:
        panel = ctk.CTkFrame(
            self.container, fg_color=CARD, corner_radius=14,
            border_width=1, border_color=BORDER,
        )
        panel.pack(fill="x")
        header = self._grid_row(panel, PANEL)
        header.pack(fill="x", padx=16, pady=(16, 6))
        for column, title in enumerate(TABLE_TITLES):
            self._cell(header, column, title, header=True)
        self.rows_frame = ctk.CTkFrame(panel, fg_color="transparent")
        self.rows_frame.pack(fill="x", padx=16)
        self.table_note = ctk.CTkLabel(
            panel, text="", font=font(11), text_color=TEXT_MUTED,
            anchor="w", wraplength=820, justify="left",
        )
        self.table_note.pack(fill="x", padx=20, pady=(0, 16))

    def _build_notes(self) -> None:
        notes = (
            "A USB barcode scanner works like a keyboard: it types the digits "
            "and presses Enter on the Point of Sale screen. No extra software "
            "or internet connection is needed.",
            "Physical stock is changed only on the Inventory screen. Editing "
            "a price, barcode or category here never changes stock levels.",
            "Deactivating a product hides it from the scanner but keeps every "
            "past receipt and stock movement intact.",
        )
        for index, note in enumerate(notes):
            ctk.CTkLabel(
                self.container,
                text="·  " + note,
                font=font(11),
                text_color=TEXT_MUTED,
                anchor="w",
                wraplength=820,
                justify="left",
            ).pack(anchor="w", pady=(10 if index == 0 else 2,
                                     12 if index == len(notes) - 1 else 0))

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------
    def _message(self, text: str, *, error: bool = False) -> None:
        self.status_label.configure(
            text=text, text_color=DANGER if error else SUCCESS
        )

    def refresh(self) -> None:
        """Header Refresh button re-reads every product from SQLite."""
        if not self.allowed:
            return
        try:
            rows = products.list_products(
                self.search_entry.get(),
                self.category_menu.get(),
            )
            counts = products.summary()
        except (ValueError, sqlite3.Error, permissions.PermissionDenied) as exc:
            self._message(str(exc), error=True)
            return
        self._render_summary(counts)
        self._render_rows(rows)

    def _render_summary(self, counts: dict[str, Any]) -> None:
        for child in self.summary_frame.winfo_children():
            child.destroy()
        cards = (
            ("Products", str(counts["products"]), "Active barcoded items", ACCENT),
            ("Categories", str(counts["categories"]), "Departments in store", TEXT_MUTED),
            ("Running low", str(counts["low_stock"]), "At or below minimum", WARNING),
            ("Out of stock", str(counts["out_of_stock"]), "Cannot be scanned", DANGER),
        )
        for column, (label, value, note, accent) in enumerate(cards):
            self.summary_frame.grid_columnconfigure(column, weight=1)
            card = ctk.CTkFrame(
                self.summary_frame, fg_color=CARD, corner_radius=14,
                border_width=1, border_color=BORDER,
            )
            card.grid(row=0, column=column, sticky="nsew", padx=(0, 12))
            ctk.CTkFrame(card, fg_color=accent, height=3, corner_radius=2).pack(
                fill="x", padx=18, pady=(16, 0)
            )
            ctk.CTkLabel(
                card, text=label, font=font(11), text_color=TEXT_MUTED,
                anchor="w",
            ).pack(fill="x", padx=18, pady=(12, 0))
            ctk.CTkLabel(
                card, text=value, font=font(21, "bold"), text_color=TEXT,
                anchor="w",
            ).pack(fill="x", padx=18, pady=(2, 0))
            ctk.CTkLabel(
                card, text=note, font=font(11), text_color=TEXT_MUTED,
                anchor="w",
            ).pack(fill="x", padx=18, pady=(2, 16))

    def _render_rows(self, rows: list[dict[str, Any]]) -> None:
        for child in self.rows_frame.winfo_children():
            child.destroy()
        for item in rows:
            row = self._grid_row(self.rows_frame, CARD)
            row.pack(fill="x", pady=2)
            on_hand = float(item["quantity_on_hand"])
            minimum = float(item["minimum_stock"])
            low = on_hand <= minimum
            self._cell(row, 0, str(item["name"]))
            self._cell(row, 1, str(item["barcode"]), color=TEXT_MUTED)
            self._cell(row, 2, str(item["category"]))
            self._cell(row, 3, format_money(item["unit_price"]))
            self._cell(
                row, 4, format_number(on_hand) + " " + str(item["unit"]),
                color=DANGER if on_hand == 0 else WARNING if low else SUCCESS,
            )
            self._cell(row, 5, format_number(minimum), color=TEXT_MUTED)
            self._cell(
                row, 6, str(item["expiration_date"] or "—"), color=TEXT_MUTED
            )
            actions = ctk.CTkFrame(row, fg_color="transparent")
            actions.grid(row=0, column=7, sticky="w", padx=4, pady=5)
            ctk.CTkButton(
                actions,
                text="Edit",
                command=lambda target=item: self._edit_product(target),
                width=52,
                height=24,
                corner_radius=6,
                fg_color=PANEL,
                hover_color="#F0E9DD",
                border_width=1,
                border_color=BORDER,
                text_color=TEXT,
                font=font(10),
            ).pack(side="left", padx=(0, 4))
            ctk.CTkButton(
                actions,
                text="Deactivate",
                command=lambda target=item: self._deactivate(target),
                width=88,
                height=24,
                corner_radius=6,
                fg_color=DANGER,
                hover_color="#8F2D21",
                text_color="#FFFFFF",
                font=font(10),
            ).pack(side="left")
        self.table_note.configure(
            text="Showing " + str(len(rows)) + " product(s). Prices are "
                 "Philippine peso (PHP)."
        )

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def _after_save(self, message: str) -> None:
        self._message(message)
        self.refresh()

    def _add_product(self) -> None:
        self._message("")
        self.dialog = ProductDialog(self, self.actor, self._after_save)

    def _edit_product(self, target: dict[str, Any]) -> None:
        self._message("")
        self.dialog = ProductDialog(self, self.actor, self._after_save, target)

    def _deactivate(self, target: dict[str, Any]) -> None:
        answer = messagebox.askyesno(
            "Deactivate product",
            "Deactivate '" + str(target["name"]) + "'?\n\n"
            "It stays in the database with its history, but the barcode will "
            "no longer work at the Point of Sale.",
        )
        if not answer:
            return
        try:
            products.deactivate_product(
                target["ingredient_id"], actor=self.actor
            )
        except (ValueError, sqlite3.Error, permissions.PermissionDenied) as exc:
            self._message(str(exc), error=True)
            return
        self._message("'" + str(target["name"]) + "' was deactivated.")
        self.refresh()

    # ------------------------------------------------------------------
    # Shared row helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _grid_row(parent: Any, background: str) -> ctk.CTkFrame:
        row = ctk.CTkFrame(parent, fg_color=background, corner_radius=8)
        for index, weight in enumerate(TABLE_WEIGHTS):
            row.grid_columnconfigure(
                index, weight=weight, minsize=TABLE_MINSIZES[index],
                uniform="products_table",
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
            wraplength=130,
            justify="left",
        ).grid(row=0, column=column, sticky="w", padx=6, pady=7)
