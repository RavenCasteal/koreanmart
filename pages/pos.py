"""
Stage 9  - Point of Sale screen.
Stage 11b - cashier picks ONE dish per sale (no cart, no barcode scanner).

The cashier chooses a dish, enters servings + payment, previews the exact
ingredient deductions and the peso total, then CONFIRMS. Only confirm writes
anything: sales_repository.confirm_sale() performs one atomic transaction and
the receipt is generated/printed afterwards. Cancelling the preview or the
confirmation dialog writes zero rows. The chosen dish's name and price are
snapshotted onto the sale, so a later rename never rewrites an old receipt.

A void after commit never deletes history: it marks the sale voided and
restores stock through a compensating stock-in (see sales_repository.py).

All SQL lives in sales_repository.py; all printing lives in receipt.py.
This file only collects input, renders results and opens dialogs.
Money is ALWAYS formatted with formatting.format_money() (PHP / peso sign).
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable

import customtkinter as ctk

import permissions
import receipt
import sales_repository as sales
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
from formatting import format_money, format_number, format_quantity

PRESET_SERVINGS = (1, 2, 5, 10)

PREVIEW_WEIGHTS = (3, 2, 2, 2, 2)
PREVIEW_MINSIZES = (170, 110, 110, 110, 110)
PREVIEW_TITLES = ("Ingredient", "Per serving", "Required", "On hand", "Status")

# Stage 11b: sales history must show WHICH dish was sold - several dishes now
# share the same counter. The uniform/minsize group keeps every row aligned.
HISTORY_WEIGHTS = (3, 3, 3, 1, 2, 2, 2, 3)
HISTORY_MINSIZES = (135, 130, 130, 45, 90, 80, 80, 145)
HISTORY_TITLES = (
    "Transaction", "Dish", "Date / time", "Qty", "Total", "Payment",
    "Status", "Actions",
)

STATUS_FILTERS = ("All", "Completed", "Voided")

VERDICT_OK_BG = "#EAF4EE"
VERDICT_BAD_BG = "#FBEAE6"

# CustomTkinter does NOT dim a CTkButton when state="disabled". Without an
# explicit muted colour the green Confirm button looks clickable before a
# valid preview, which is confusing on a shared counter PC.
CONFIRM_DISABLED_COLOR = "#A9B8AE"


# ----------------------------------------------------------------------
# Dialogs
# ----------------------------------------------------------------------
class PriceDialog(ModalDialog):
    """Edit ONE dish's unit price in PHP (manager only)."""

    def __init__(
        self,
        master: Any,
        price: float,
        on_saved: Callable[[], None],
        actor: Any = None,
        menu_item_id: Any = None,
        dish_name: str = "this dish",
    ):
        super().__init__(master, "Unit price", 420)
        self.on_saved = on_saved
        self.actor = actor
        self.menu_item_id = menu_item_id
        self.heading(
            "Unit price",
            f"Price for {dish_name}, in Philippine peso. Other dishes keep "
            "their own prices, and existing sales keep the price they were "
            "recorded with.",
        )
        self.entry = self.field("Unit price (PHP)")
        self.entry.insert(0, format_number(price))
        self.error_label()
        self.actions("Save price", self._save)
        self.finish(self.entry)

    def _save(self) -> None:
        self.show_error("")
        try:
            sales.set_unit_price(
                self.entry.get(),
                actor=self.actor,
                menu_item_id=self.menu_item_id,
            )
        except (ValueError, sqlite3.Error) as exc:
            self.show_error(str(exc))
            return
        self.close()
        self.on_saved()


class ConfirmSaleDialog(ModalDialog):
    """Final confirmation: deductions + money. Cancel here writes nothing."""

    def __init__(
        self,
        master: Any,
        preview: dict[str, Any],
        on_confirm: Callable[[], tuple[bool, str]],
    ) -> None:
        super().__init__(master, "Confirm sale", 490)
        self.preview = preview
        self.on_confirm = on_confirm

        self.heading(
            "Confirm this sale",
            "Stock is deducted and the receipt is printed only when you "
            "press the button below. Cancel leaves the database untouched.",
        )
        facts = (
            ("Item", preview["menu_item_name"]),
            ("Servings", str(preview["servings"])),
            ("Unit price", format_money(preview["unit_price"])),
            ("TOTAL", format_money(preview["total_amount"])),
            ("Payment", preview["payment_method"]),
            ("Tendered", format_money(preview["amount_tendered"])),
            ("Change", format_money(preview["change_amount"])),
        )
        for label, value in facts:
            line = ctk.CTkFrame(self.card, fg_color="transparent")
            line.pack(fill="x", padx=24, pady=1)
            ctk.CTkLabel(
                line, text=label, font=font(11), text_color=TEXT_MUTED, anchor="w"
            ).pack(side="left")
            ctk.CTkLabel(
                line,
                text=value,
                font=font(12, "bold") if label == "TOTAL" else font(12),
                text_color=ACCENT if label == "TOTAL" else TEXT,
                anchor="e",
            ).pack(side="right")

        ctk.CTkLabel(
            self.card,
            text="Ingredient deductions",
            font=font(12, "bold"),
            text_color=TEXT,
            anchor="w",
        ).pack(fill="x", padx=24, pady=(16, 4))

        box = ctk.CTkScrollableFrame(
            self.card,
            height=170,
            fg_color=PANEL,
            corner_radius=10,
            border_width=1,
            border_color=BORDER,
        )
        box.pack(fill="x", padx=24)
        for line in preview["lines"]:
            row = ctk.CTkFrame(box, fg_color="transparent")
            row.pack(fill="x", pady=2)
            ctk.CTkLabel(
                row,
                text=line["name"],
                font=font(11),
                text_color=TEXT,
                anchor="w",
            ).pack(side="left")
            ctk.CTkLabel(
                row,
                text=format_quantity(line["quantity_required"], line["unit"]),
                font=font(11),
                text_color=TEXT_MUTED,
                anchor="e",
            ).pack(side="right")

        self.error_label()
        self.actions("Confirm sale & print receipt", self._save, cancel_text="Cancel")
        self.finish()

    def _save(self) -> None:
        self.show_error("")
        ok, message = self.on_confirm()
        if not ok:
            self.show_error(message)
            return
        self.close()


class VoidDialog(ModalDialog):
    """Reason for voiding an already-completed sale."""

    def __init__(
        self,
        master: Any,
        transaction_number: str,
        on_confirm: Callable[[str], tuple[bool, str]],
    ) -> None:
        super().__init__(master, "Void sale", 440)
        self.on_confirm = on_confirm
        self.heading(
            "Void this sale",
            f"{transaction_number} will be marked VOIDED (never deleted) and "
            "the deducted ingredients will be restored with a stock-in.",
        )
        self.reason = self.field("Reason for voiding")
        self.error_label()
        self.actions("Void sale", self._save, cancel_text="Cancel", danger=True)
        self.finish(self.reason)

    def _save(self) -> None:
        self.show_error("")
        ok, message = self.on_confirm(self.reason.get())
        if not ok:
            self.show_error(message)
            return
        self.close()


class ReceiptDialog(ModalDialog):
    """Print preview + reprint. The receipt text is already archived."""

    def __init__(
        self,
        master: Any,
        text: str,
        print_detail: str,
        on_print: Callable[[], str],
    ) -> None:
        super().__init__(master, "Receipt", 430)
        self.on_print = on_print
        self.heading("Receipt", print_detail)
        self.textbox = ctk.CTkTextbox(
            self.card,
            height=360,
            corner_radius=10,
            fg_color=PANEL,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT,
            font=ctk.CTkFont(family="Consolas", size=11),
        )
        self.textbox.pack(fill="x", padx=24)
        self.textbox.insert("0.0", text)
        self.textbox.configure(state="disabled")
        self.error_label()
        self.actions("Print again", self._print, cancel_text="Close")
        self.finish()

    def _print(self) -> None:
        self.show_error("")
        detail = self.on_print()
        self.show_error(detail)


# ----------------------------------------------------------------------
# Main POS page
# ----------------------------------------------------------------------
class PosView(ctk.CTkFrame):
    """New sale form + live deduction preview + recent sales history."""

    def __init__(
        self,
        master: Any,
        user: dict[str, Any] | None = None,
        on_navigate: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(master, fg_color=CREAM, corner_radius=0)
        self.user = user or {}
        self.on_navigate = on_navigate
        self.context: dict[str, Any] = {}
        self.preview: dict[str, Any] | None = None
        self.pending_receipt: dict[str, Any] | None = None
        # Stage 11b: the dish currently chosen in the dropdown. None means
        # "use the primary POS dish", which is also the Stage 1-11a default.
        self.selected_id: int | None = None
        self.dish_names: dict[str, int] = {}

        self.scroller = ctk.CTkScrollableFrame(self, fg_color=CREAM, corner_radius=0)
        self.scroller.pack(fill="both", expand=True)
        self.container = ctk.CTkFrame(self.scroller, fg_color="transparent")
        self.container.pack(fill="both", expand=True, padx=26, pady=22)

        self._build_toolbar()
        self.summary_frame = ctk.CTkFrame(self.container, fg_color="transparent")
        self.summary_frame.pack(fill="x", pady=(0, 14))
        self._build_sale_card()
        self._build_preview_card()
        self._build_history_card()
        self._build_footer()
        self.refresh()

    # ------------------------------------------------------------------
    # Static layout
    # ------------------------------------------------------------------
    def _build_toolbar(self) -> None:
        top = ctk.CTkFrame(self.container, fg_color="transparent")
        top.pack(fill="x", pady=(0, 12))
        left = ctk.CTkFrame(top, fg_color="transparent")
        left.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(
            left, text="Point of sale", font=font(20, "bold"), text_color=TEXT
        ).pack(anchor="w")
        self.context_label = ctk.CTkLabel(
            left, text="", font=font(11), text_color=TEXT_MUTED, anchor="w"
        )
        self.context_label.pack(anchor="w")
        # Stage 10: only a manager may change the price. A cashier gets no
        # button at all, and _edit_price() checks the capability again.
        self.price_button: Any = None
        if permissions.can(self.user, permissions.CAP_SET_PRICE):
            self.price_button = ctk.CTkButton(
                top,
                text="Edit unit price",
                command=self._edit_price,
                width=140,
                height=32,
                corner_radius=8,
                fg_color=PANEL,
                hover_color="#F0E9DD",
                border_width=1,
                border_color=BORDER,
                text_color=TEXT_MUTED,
                font=font(11),
            )
            self.price_button.pack(side="right", padx=(12, 0))
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
        self.status_label.pack(fill="x", pady=(0, 10))

    def _build_sale_card(self) -> None:
        card = ctk.CTkFrame(
            self.container,
            fg_color=CARD,
            corner_radius=14,
            border_width=1,
            border_color=BORDER,
        )
        card.pack(fill="x", pady=(0, 14))
        ctk.CTkLabel(
            card,
            text="New sale  ·  one dish per sale",
            font=font(14, "bold"),
            text_color=TEXT,
        ).pack(anchor="w", padx=20, pady=(16, 2))
        ctk.CTkLabel(
            card,
            text="Choose a dish, then preview. Stock is deducted only after "
                 "you confirm.",
            font=font(11),
            text_color=TEXT_MUTED,
            anchor="w",
        ).pack(anchor="w", padx=20)

        # Stage 11b - dish picker. Every ACTIVE dish is sellable; the primary
        # dish is listed first by sales_repository.list_sellable_dishes().
        # No barcode / SKU lookup: this is a plain name dropdown.
        dish_row = ctk.CTkFrame(card, fg_color="transparent")
        dish_row.pack(fill="x", padx=20, pady=(14, 0))
        ctk.CTkLabel(
            dish_row, text="Dish", font=font(11, "bold"), text_color=TEXT
        ).pack(side="left", padx=(0, 8))
        self.dish_combo = ctk.CTkComboBox(
            dish_row,
            values=["Regular Ramen"],
            width=270,
            height=38,
            corner_radius=9,
            fg_color=PANEL,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT,
            font=font(13, "bold"),
            dropdown_font=font(12),
            command=self._on_dish_change,
        )
        self.dish_combo.pack(side="left")
        self.dish_combo.set("Regular Ramen")
        self.dish_price_label = ctk.CTkLabel(
            dish_row, text="", font=font(11), text_color=TEXT_MUTED
        )
        self.dish_price_label.pack(side="left", padx=(14, 0))

        first = ctk.CTkFrame(card, fg_color="transparent")
        first.pack(fill="x", padx=20, pady=(12, 0))
        ctk.CTkLabel(
            first, text="Servings", font=font(11, "bold"), text_color=TEXT
        ).pack(side="left", padx=(0, 8))
        self.servings_entry = ctk.CTkEntry(
            first,
            width=90,
            height=38,
            corner_radius=9,
            fg_color=PANEL,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT,
            font=font(14, "bold"),
            justify="center",
        )
        self.servings_entry.pack(side="left")
        self.servings_entry.insert(0, "1")
        self.servings_entry.bind("<Return>", lambda _event: self._run_preview())
        for value in PRESET_SERVINGS:
            ctk.CTkButton(
                first,
                text=str(value),
                command=lambda count=value: self._use_preset(count),
                width=40,
                height=30,
                corner_radius=8,
                fg_color=PANEL,
                hover_color="#F0E9DD",
                border_width=1,
                border_color=BORDER,
                text_color=TEXT_MUTED,
                font=font(11),
            ).pack(side="left", padx=(6, 0))
        self.max_label = ctk.CTkLabel(
            first, text="", font=font(11), text_color=TEXT_MUTED
        )
        self.max_label.pack(side="right")

        second = ctk.CTkFrame(card, fg_color="transparent")
        second.pack(fill="x", padx=20, pady=(12, 0))
        ctk.CTkLabel(
            second, text="Payment", font=font(11, "bold"), text_color=TEXT
        ).pack(side="left", padx=(0, 8))
        self.payment_combo = ctk.CTkComboBox(
            second,
            values=list(sales.PAYMENT_METHODS),
            width=130,
            height=38,
            corner_radius=9,
            fg_color=PANEL,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT,
            font=font(12),
            dropdown_font=font(12),
            command=lambda _value: self._payment_changed(),
        )
        self.payment_combo.pack(side="left")
        self.payment_combo.set(sales.PAYMENT_METHODS[0])
        ctk.CTkLabel(
            second, text="Amount tendered", font=font(11, "bold"), text_color=TEXT
        ).pack(side="left", padx=(18, 8))
        self.tendered_entry = ctk.CTkEntry(
            second,
            width=120,
            height=38,
            corner_radius=9,
            fg_color=PANEL,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT,
            font=font(13),
            justify="right",
        )
        self.tendered_entry.pack(side="left")
        self.tendered_entry.bind("<Return>", lambda _event: self._run_preview())
        ctk.CTkButton(
            second,
            text="Exact total",
            command=self._use_exact_total,
            width=100,
            height=30,
            corner_radius=8,
            fg_color=PANEL,
            hover_color="#F0E9DD",
            border_width=1,
            border_color=BORDER,
            text_color=TEXT_MUTED,
            font=font(11),
        ).pack(side="left", padx=(8, 0))

        actions = ctk.CTkFrame(card, fg_color="transparent")
        actions.pack(fill="x", padx=20, pady=(14, 0))
        ctk.CTkButton(
            actions,
            text="Preview sale",
            command=self._run_preview,
            width=150,
            height=38,
            corner_radius=9,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            text_color="#FFFFFF",
            font=font(12, "bold"),
        ).pack(side="left")
        self.confirm_button = ctk.CTkButton(
            actions,
            text="Confirm sale & print receipt",
            command=self._open_confirm,
            width=220,
            height=38,
            corner_radius=9,
            fg_color=CONFIRM_DISABLED_COLOR,
            hover_color=CONFIRM_DISABLED_COLOR,
            text_color="#FFFFFF",
            font=font(12, "bold"),
            state="disabled",
        )
        self.confirm_button.pack(side="left", padx=10)
        ctk.CTkButton(
            actions,
            text="Clear",
            command=self._clear_form,
            width=90,
            height=38,
            corner_radius=9,
            fg_color=PANEL,
            hover_color="#F0E9DD",
            border_width=1,
            border_color=BORDER,
            text_color=TEXT_MUTED,
            font=font(12),
        ).pack(side="left")
        self.form_error = ctk.CTkLabel(
            card,
            text="",
            font=font(11),
            text_color=DANGER,
            anchor="w",
            wraplength=820,
            justify="left",
            height=24,
        )
        self.form_error.pack(fill="x", padx=20, pady=(8, 14))

    def _build_preview_card(self) -> None:
        self.preview_card = ctk.CTkFrame(
            self.container,
            fg_color=CARD,
            corner_radius=14,
            border_width=1,
            border_color=BORDER,
        )
        self.preview_card.pack(fill="x", pady=(0, 14))
        head = ctk.CTkFrame(self.preview_card, fg_color="transparent")
        head.pack(fill="x", padx=20, pady=(14, 4))
        ctk.CTkLabel(
            head, text="Sale preview", font=font(14, "bold"), text_color=TEXT
        ).pack(side="left")
        ctk.CTkLabel(
            head,
            text="Read-only · nothing is deducted yet",
            font=font(11),
            text_color=TEXT_MUTED,
        ).pack(side="right")
        self.preview_verdict = ctk.CTkFrame(
            self.preview_card, fg_color=VERDICT_BAD_BG, corner_radius=10
        )
        self.preview_verdict.pack(fill="x", padx=16, pady=(6, 8))
        self.preview_verdict_label = ctk.CTkLabel(
            self.preview_verdict,
            text="Enter servings and press Preview sale.",
            font=font(12, "bold"),
            text_color=TEXT_MUTED,
            anchor="w",
            wraplength=800,
            justify="left",
        )
        self.preview_verdict_label.pack(anchor="w", padx=16, pady=(10, 2))
        self.preview_info = ctk.CTkLabel(
            self.preview_verdict,
            text="",
            font=font(11),
            text_color=TEXT_MUTED,
            anchor="w",
            wraplength=800,
            justify="left",
        )
        self.preview_info.pack(anchor="w", padx=16, pady=(0, 10))

        header = self._grid_row(self.preview_card, PREVIEW_WEIGHTS, PREVIEW_MINSIZES,
                                "pos_preview", PANEL)
        header.pack(fill="x", padx=16, pady=(6, 4))
        for column, title in enumerate(PREVIEW_TITLES):
            self._cell(header, column, title, header=True)
        self.preview_rows = ctk.CTkFrame(self.preview_card, fg_color="transparent")
        self.preview_rows.pack(fill="x", padx=16, pady=(0, 8))
        self.preview_note = ctk.CTkLabel(
            self.preview_card,
            text="",
            font=font(11),
            text_color=TEXT_MUTED,
            anchor="w",
            wraplength=820,
            justify="left",
        )
        self.preview_note.pack(fill="x", padx=20, pady=(0, 14))

    def _build_history_card(self) -> None:
        panel = ctk.CTkFrame(
            self.container,
            fg_color=CARD,
            corner_radius=14,
            border_width=1,
            border_color=BORDER,
        )
        panel.pack(fill="x")
        head = ctk.CTkFrame(panel, fg_color="transparent")
        head.pack(fill="x", padx=20, pady=(14, 8))
        ctk.CTkLabel(
            head, text="Recent sales", font=font(14, "bold"), text_color=TEXT
        ).pack(side="left")
        self.history_search = ctk.CTkEntry(
            head,
            width=180,
            height=32,
            corner_radius=8,
            fg_color=PANEL,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT,
            font=font(11),
            placeholder_text="Search transaction…",
        )
        self.history_search.pack(side="right", padx=(8, 0))
        self.history_search.bind("<KeyRelease>", lambda _event: self._render_history())
        self.status_combo = ctk.CTkComboBox(
            head,
            values=STATUS_FILTERS,
            width=120,
            height=32,
            corner_radius=8,
            fg_color=PANEL,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT,
            font=font(11),
            dropdown_font=font(11),
            command=lambda _value: self._render_history(),
        )
        self.status_combo.pack(side="right")
        self.status_combo.set("All")
        ctk.CTkButton(
            head,
            text="Clear",
            command=self._clear_history_filters,
            width=64,
            height=30,
            corner_radius=8,
            fg_color=PANEL,
            hover_color="#F0E9DD",
            border_width=1,
            border_color=BORDER,
            text_color=TEXT_MUTED,
            font=font(11),
        ).pack(side="right", padx=(0, 8))

        header = self._grid_row(panel, HISTORY_WEIGHTS, HISTORY_MINSIZES,
                                "pos_history", PANEL)
        header.pack(fill="x", padx=16, pady=(4, 6))
        for column, title in enumerate(HISTORY_TITLES):
            self._cell(header, column, title, header=True)
        self.history_rows = ctk.CTkFrame(panel, fg_color="transparent")
        self.history_rows.pack(fill="x", padx=16, pady=(0, 8))
        self.history_note = ctk.CTkLabel(
            panel,
            text="",
            font=font(11),
            text_color=TEXT_MUTED,
            anchor="w",
            wraplength=820,
            justify="left",
        )
        self.history_note.pack(fill="x", padx=20, pady=(0, 14))

    def _build_footer(self) -> None:
        notes = (
            "One dish per sale. Every active dish's own recipe is used for "
            "the deduction. Amounts are in Philippine peso (PHP).",
            "Preview is read-only. Confirm writes ONE atomic transaction: "
            "sale record, ingredient deductions and stock-out movement.",
            "Insufficient stock blocks the sale with zero writes. Cancelling "
            "before Confirm leaves no record at all.",
            "A void after Confirm never deletes history - it marks the sale "
            "VOIDED and restores stock with a compensating stock-in.",
            "Receipts are archived in receipts/ and can be reprinted from "
            "the Actions column. Printer problems never undo a sale.",
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
    # Data / refresh
    # ------------------------------------------------------------------
    def refresh(self) -> None:
        """Header Refresh re-reads everything for the SELECTED dish."""
        try:
            self.context = sales.get_pos_context(
                menu_item_id=self.selected_id
            )
        except (sqlite3.Error, ValueError):
            # The chosen dish may have just been deactivated by a manager on
            # another screen. Fall back to the primary dish instead of leaving
            # the cashier with a dead form.
            try:
                self.selected_id = None
                self.context = sales.get_pos_context()
            except (sqlite3.Error, ValueError) as exc:
                self._message(str(exc), error=True)
                return
        self._render_dish_picker()
        self._render_summary()
        self._render_context()
        self._render_history()
        if self.preview is not None:
            self._run_preview(quiet=True)

    def _render_dish_picker(self) -> None:
        """Rebuild the dropdown from the active dishes in SQLite."""
        rows = self.context.get("dishes", [])
        self.dish_names = {
            str(row["name"]): int(row["menu_item_id"]) for row in rows
        }
        labels = list(self.dish_names)
        self.dish_combo.configure(values=labels or ["No dishes available"])
        current = self.context.get("menu_item_id")
        match = next(
            (name for name, key in self.dish_names.items() if key == current),
            None,
        )
        if match is not None:
            self.dish_combo.set(match)
            self.selected_id = int(current)
        elif labels:
            self.dish_combo.set(labels[0])
            self.selected_id = self.dish_names[labels[0]]
        else:
            self.dish_combo.set("No dishes available")
            self.selected_id = None
        self.dish_price_label.configure(
            text=(
                f"{format_money(self.context.get('unit_price', 0))} "
                "per serving"
            )
        )

    def _on_dish_change(self, choice: str) -> None:
        """Selecting another dish invalidates any stale preview."""
        key = self.dish_names.get(choice)
        if key is None or key == self.selected_id:
            return
        self.selected_id = key
        self.preview = None
        self._set_confirm_enabled(False)
        self.form_error.configure(text="")
        self._render_preview(None)
        self._message(f"Dish changed to {choice}. Press Preview sale.")
        self.refresh()

    def _render_context(self) -> None:
        self.context_label.configure(
            text=(
                f"{self.context['menu_item_name']}  ·  "
                f"{format_money(self.context['unit_price'])} per serving  ·  "
                f"{len(self.context['lines'])} recipe ingredients  ·  "
                "prices and balances read live from SQLite"
            )
        )
        self.max_label.configure(
            text=f"Can make now: {self.context['max_servings']} serving(s)"
        )
        # Prefill "amount tendered" with the exact price of one serving the
        # first time the page opens, so Preview works immediately.
        if not self.tendered_entry.get().strip():
            self.tendered_entry.insert(
                0, format_number(self.context.get("unit_price", 0))
            )

    def _render_summary(self) -> None:
        for child in self.summary_frame.winfo_children():
            child.destroy()
        summary = self.context.get("summary", {})
        cards = [
            ("Unit price", format_money(self.context.get("unit_price", 0)),
             "PHP · editable by the manager", ACCENT),
            ("Servings available", self.context.get("max_servings", 0),
             "Limited by the scarcest ingredient", SUCCESS),
        ]
        # Stage 10: turnover figures are management information. A cashier
        # still needs the price and the maximum servings to make a sale, but
        # does not need to see the shop's sales totals.
        if permissions.is_manager(self.user):
            cards.extend([
                ("Completed sales", summary.get("completed_sales", 0),
                 "Voided sales excluded", TEXT_MUTED),
                ("Gross sales", format_money(summary.get("gross_sales", 0)),
                 "Completed sales only", WARNING),
            ])
        for column in range(len(cards)):
            self.summary_frame.grid_columnconfigure(column, weight=1)
        for column, (label, value, note, accent) in enumerate(cards):
            self._stat_card(column, label, value, note, accent)

    def _stat_card(
        self, column: int, label: str, value: Any, note: str, accent: str
    ) -> None:
        card = ctk.CTkFrame(
            self.summary_frame,
            fg_color=CARD,
            corner_radius=14,
            border_width=1,
            border_color=BORDER,
        )
        card.grid(row=0, column=column, sticky="nsew", padx=(0, 12))
        ctk.CTkFrame(card, fg_color=accent, height=3, corner_radius=2).pack(
            fill="x", padx=18, pady=(16, 0)
        )
        ctk.CTkLabel(
            card, text=label, font=font(11), text_color=TEXT_MUTED, anchor="w"
        ).pack(fill="x", padx=18, pady=(12, 0))
        ctk.CTkLabel(
            card,
            text=str(value),
            font=font(21, "bold"),
            text_color=TEXT,
            anchor="w",
            wraplength=200,
            justify="left",
        ).pack(fill="x", padx=18, pady=(2, 0))
        ctk.CTkLabel(
            card,
            text=note,
            font=font(11),
            text_color=TEXT_MUTED,
            anchor="w",
            wraplength=200,
            justify="left",
        ).pack(fill="x", padx=18, pady=(2, 16))

    # ------------------------------------------------------------------
    # Form actions
    # ------------------------------------------------------------------
    def _message(self, text: str, *, error: bool = False) -> None:
        self.status_label.configure(
            text=text, text_color=DANGER if error else SUCCESS
        )

    def _use_preset(self, count: int) -> None:
        self.servings_entry.delete(0, "end")
        self.servings_entry.insert(0, str(count))
        self._run_preview()

    def _payment_changed(self) -> None:
        if self.payment_combo.get() != "Cash":
            self._use_exact_total()

    def _use_exact_total(self) -> None:
        """Fill tendered with the CURRENT unit price x CURRENT servings.

        Stage 10 fix: this used to reuse self.preview["total_amount"] whenever
        a preview existed. After changing servings the cached preview was
        stale, so "Exact total" prefilled the wrong amount and the confirm
        dialog rejected the sale with a short-payment error. Recomputing from
        the live SQLite price and the live entry is always correct.
        """
        try:
            count = max(1, int(float(self.servings_entry.get().strip() or "1")))
        except (TypeError, ValueError):
            count = 1
        try:
            price = sales.get_unit_price(menu_item_id=self.selected_id)
        except (ValueError, sqlite3.Error):
            price = float(self.context.get("unit_price", 0) or 0)
        total = round(price * count, 2)
        self.tendered_entry.delete(0, "end")
        self.tendered_entry.insert(0, format_number(total))
        self._run_preview(quiet=True)

    def _set_confirm_enabled(self, enabled: bool) -> None:
        """Enable/disable Confirm AND make it visibly look disabled.

        A disabled CTkButton still renders with its normal fg_color, so the
        colour is switched here as well. All state changes go through this
        helper so the affordance and the actual behaviour never disagree.
        """
        colour = SUCCESS if enabled else CONFIRM_DISABLED_COLOR
        self.confirm_button.configure(
            state="normal" if enabled else "disabled",
            fg_color=colour,
            hover_color="#326349" if enabled else CONFIRM_DISABLED_COLOR,
        )

    def _clear_form(self) -> None:
        self.servings_entry.delete(0, "end")
        self.servings_entry.insert(0, "1")
        self.tendered_entry.delete(0, "end")
        self.payment_combo.set(sales.PAYMENT_METHODS[0])
        self.preview = None
        self._set_confirm_enabled(False)
        self.form_error.configure(text="")
        self._render_preview(None)
        self._message("Form cleared. Nothing was written.")

    def _run_preview(self, quiet: bool = False) -> None:
        try:
            result = sales.preview_sale(
                self.servings_entry.get(),
                self.payment_combo.get(),
                self.tendered_entry.get(),
                menu_item_id=self.selected_id,
            )
        except (ValueError, sqlite3.Error) as exc:
            self.preview = None
            self._set_confirm_enabled(False)
            if not quiet:
                self.form_error.configure(text=str(exc))
                self._message(str(exc), error=True)
            self._render_preview(None)
            return
        self.preview = result
        self.form_error.configure(text="")
        self._render_preview(result)
        if result["sufficient"]:
            self._set_confirm_enabled(True)
            if not quiet:
                self._message(
                    f"Enough stock for {result['servings']} serving(s). "
                    "Review the deductions, then confirm."
                )
        else:
            self._set_confirm_enabled(False)
            short = ", ".join(
                f"{row['name']} ({format_quantity(row['shortfall'], row['unit'])})"
                for row in result["shortages"][:3]
            )
            if not quiet:
                self._message(
                    f"Not enough stock. Short by: {short}. "
                    f"Maximum now: {result['max_servings']} serving(s).",
                    error=True,
                )

    def _render_preview(self, result: dict[str, Any] | None) -> None:
        for child in self.preview_rows.winfo_children():
            child.destroy()
        if result is None:
            self.preview_card.configure(fg_color=CARD)
            self.preview_verdict.configure(fg_color=VERDICT_BAD_BG)
            self.preview_verdict_label.configure(
                text="Enter servings and press Preview sale.",
                text_color=TEXT_MUTED,
            )
            self.preview_info.configure(text="")
            self.preview_note.configure(text="")
            return

        total_text = (
            f"{result['servings']} × {format_money(result['unit_price'])} = "
            f"{format_money(result['total_amount'])}  ·  "
            f"{result['payment_method']}  ·  tendered "
            f"{format_money(result['amount_tendered'])}  ·  change "
            f"{format_money(result['change_amount'])}"
        )
        if result["sufficient"]:
            self.preview_verdict.configure(fg_color=VERDICT_OK_BG)
            self.preview_verdict_label.configure(
                text=f"Enough stock for {result['servings']} serving(s)",
                text_color=SUCCESS,
            )
        else:
            self.preview_verdict.configure(fg_color=VERDICT_BAD_BG)
            self.preview_verdict_label.configure(
                text=(
                    f"Cannot sell {result['servings']} serving(s) - "
                    f"{len(result['shortages'])} ingredient(s) short"
                ),
                text_color=DANGER,
            )
        self.preview_info.configure(text=total_text)
        for line in result["lines"]:
            row = self._grid_row(
                self.preview_rows, PREVIEW_WEIGHTS, PREVIEW_MINSIZES,
                "pos_preview", CARD,
            )
            row.pack(fill="x", pady=2)
            self._cell(row, 0, line["name"])
            self._cell(row, 1, format_quantity(
                line["quantity_per_serving"], line["unit"]))
            self._cell(row, 2, format_quantity(
                line["quantity_required"], line["unit"]))
            self._cell(row, 3, format_quantity(
                line["quantity_on_hand"], line["unit"]))
            ok = line["sufficient"]
            badge = ctk.CTkLabel(
                row,
                text="Enough" if ok else f"Short {format_quantity(line['shortfall'], '')}",
                fg_color=SUCCESS if ok else DANGER,
                text_color="#FFFFFF",
                corner_radius=6,
                width=100,
                height=22,
                font=font(11, "bold"),
            )
            badge.grid(row=0, column=4, sticky="w", padx=6, pady=7)
        self.preview_note.configure(
            text=(
                f"Required = recipe quantity × servings. Maximum complete "
                f"servings right now: {result['max_servings']}. "
                "This preview does not deduct anything."
            )
        )

    # ------------------------------------------------------------------
    # Confirm / receipt
    # ------------------------------------------------------------------
    def _open_confirm(self) -> None:
        if self.preview is None or not self.preview["sufficient"]:
            self._message("Run a sufficient preview before confirming.", error=True)
            return
        ConfirmSaleDialog(self, self.preview, self._commit_sale)

    def _commit_sale(self) -> tuple[bool, str]:
        """Called by the confirmation dialog. Returns (ok, error message)."""
        if self.preview is None:
            return False, "The preview expired. Press Preview sale again."
        try:
            result = sales.confirm_sale(
                self.servings_entry.get(),
                self.payment_combo.get(),
                self.tendered_entry.get(),
                user_id=self.user.get("user_id"),
                actor=self.user,
                menu_item_id=self.selected_id,
            )
        except (ValueError, sqlite3.Error) as exc:
            return False, str(exc)

        # Printing is deliberately AFTER the commit. A printer failure can
        # never roll back the sale or lose a deduction.
        try:
            issued = receipt.issue_receipt(result)
        except (OSError, ValueError) as exc:
            issued = {
                "text": receipt.build_receipt(result),
                "print": {"detail": f"Receipt could not be issued: {exc}"},
            }
        self.pending_receipt = {"sale": result, "issued": issued}
        self._message(
            f"Sale {result['transaction_number']} recorded. "
            f"{len(result['lines'])} ingredient(s) deducted. "
            + str(issued["print"]["detail"])
        )
        self.preview = None
        self._set_confirm_enabled(False)
        self.after(220, self._show_pending_receipt)
        self.refresh()
        return True, ""

    def _show_pending_receipt(self) -> None:
        if self.pending_receipt is None:
            return
        payload = self.pending_receipt
        self.pending_receipt = None
        issued = payload["issued"]
        ReceiptDialog(
            self,
            issued["text"],
            str(issued["print"]["detail"]),
            lambda: self._print_again(payload["sale"]),
        )

    def _print_again(self, sale: dict[str, Any]) -> str:
        try:
            issued = receipt.issue_receipt(sale, reprint=True)
            return "Reprint: " + str(issued["print"]["detail"])
        except (OSError, ValueError) as exc:
            return f"Could not reprint: {exc}"

    def _reprint(self, sale_id: int) -> None:
        try:
            detail = sales.get_sale_detail(sale_id)
            issued = receipt.issue_receipt(detail, reprint=True)
        except (ValueError, sqlite3.Error, OSError) as exc:
            self._message(f"Could not open the receipt: {exc}", error=True)
            return
        ReceiptDialog(
            self,
            issued["text"],
            str(issued["print"]["detail"]),
            lambda: self._print_again(detail),
        )

    # ------------------------------------------------------------------
    # Void / price
    # ------------------------------------------------------------------
    def _open_void(self, sale_id: int, transaction_number: str) -> None:
        if not permissions.can(self.user, permissions.CAP_VOID_SALE):
            self._message(
                "Only a manager can void a sale.", error=True
            )
            return
        VoidDialog(
            self,
            transaction_number,
            lambda reason: self._void(sale_id, reason),
        )

    def _void(self, sale_id: int, reason: str) -> tuple[bool, str]:
        try:
            result = sales.void_sale(
                sale_id, reason,
                user_id=self.user.get("user_id"),
                actor=self.user,
            )
            issued = receipt.issue_receipt(result)
        except (ValueError, sqlite3.Error, OSError) as exc:
            return False, str(exc)
        self._message(
            f"Sale {result['transaction_number']} voided. "
            f"{len(result['lines'])} ingredient(s) restored. "
            + str(issued["print"]["detail"])
        )
        self.after(
            220,
            lambda: ReceiptDialog(
                self,
                issued["text"],
                str(issued["print"]["detail"]),
                lambda: self._print_again(result),
            ),
        )
        self.refresh()
        return True, ""

    def _edit_price(self) -> None:
        if not permissions.can(self.user, permissions.CAP_SET_PRICE):
            self._message("Only a manager can change the price.", error=True)
            return
        PriceDialog(
            self,
            self.context.get("unit_price", 0),
            self.refresh,
            actor=self.user,
            menu_item_id=self.selected_id,
            dish_name=str(self.context.get("menu_item_name", "this dish")),
        )

    # ------------------------------------------------------------------
    # Sales history
    # ------------------------------------------------------------------
    def _clear_history_filters(self) -> None:
        self.history_search.delete(0, "end")
        self.status_combo.set("All")
        self._render_history()

    def _history_data(self) -> dict[str, Any]:
        choice = self.status_combo.get()
        status = (
            "completed" if choice == "Completed"
            else "voided" if choice == "Voided"
            else "ALL"
        )
        return sales.get_sales_history(
            status=status, search=self.history_search.get()
        )

    def _render_history(self) -> None:
        try:
            data = self._history_data()
        except (ValueError, sqlite3.Error) as exc:
            self.history_note.configure(text=str(exc))
            return
        for child in self.history_rows.winfo_children():
            child.destroy()
        rows = data["rows"]
        if not rows:
            empty = ctk.CTkFrame(self.history_rows, fg_color=PANEL, corner_radius=10)
            empty.pack(fill="x", pady=(8, 12))
            ctk.CTkLabel(
                empty,
                text="No sales recorded yet",
                font=font(14, "bold"),
                text_color=TEXT,
            ).pack(pady=(18, 2))
            ctk.CTkLabel(
                empty,
                text="Confirm a sale above and it will appear here.",
                font=font(11),
                text_color=TEXT_MUTED,
            ).pack(pady=(0, 18))
        for item in rows:
            row = self._grid_row(
                self.history_rows, HISTORY_WEIGHTS, HISTORY_MINSIZES,
                "pos_history", CARD,
            )
            row.pack(fill="x", pady=2)
            voided = item["status"] == "voided"
            self._cell(row, 0, item["transaction_number"])
            self._cell(row, 1, item["menu_item_name"])
            self._cell(row, 2, item["transaction_date"])
            self._cell(row, 3, str(item["quantity"]))
            self._cell(row, 4, format_money(item["total_amount"]))
            self._cell(row, 5, item["payment_method"])
            self._cell(
                row, 6,
                "VOIDED" if voided else "Completed",
                color=DANGER if voided else SUCCESS,
            )
            actions = ctk.CTkFrame(row, fg_color="transparent")
            actions.grid(row=0, column=7, sticky="w", padx=4, pady=5)
            ctk.CTkButton(
                actions,
                text="Receipt",
                command=lambda key=int(item["sale_id"]): self._reprint(key),
                width=72,
                height=24,
                corner_radius=6,
                fg_color=PANEL,
                hover_color="#F0E9DD",
                border_width=1,
                border_color=BORDER,
                text_color=TEXT_MUTED,
                font=font(10),
            ).pack(side="left", padx=(0, 4))
            if not voided and permissions.can(
                self.user, permissions.CAP_VOID_SALE
            ):
                ctk.CTkButton(
                    actions,
                    text="Void",
                    command=lambda key=int(item["sale_id"]),
                                   number=str(item["transaction_number"]):
                        self._open_void(key, number),
                    width=56,
                    height=24,
                    corner_radius=6,
                    fg_color=DANGER,
                    hover_color="#8F2D21",
                    text_color="#FFFFFF",
                    font=font(10),
                ).pack(side="left")
        self.history_note.configure(
            text=(
                f"Showing {len(rows)} of {data['filtered']} matching sale(s) "
                f"(newest first, limit {data['limit']}). "
                f"Total recorded: {data['summary']['total_sales']}; "
                f"voided: {data['summary']['voided_sales']}. "
                "Voided sales are kept for audit and never deleted."
            )
        )

    # ------------------------------------------------------------------
    # Shared row / cell helpers (identical minsize + uniform on every row)
    # ------------------------------------------------------------------
    @staticmethod
    def _grid_row(
        parent: Any,
        weights: tuple[int, ...],
        minsizes: tuple[int, ...],
        group: str,
        background: str,
    ) -> ctk.CTkFrame:
        row = ctk.CTkFrame(parent, fg_color=background, corner_radius=8)
        for index, weight in enumerate(weights):
            row.grid_columnconfigure(
                index,
                weight=weight,
                minsize=minsizes[index],
                uniform=group,
            )
        return row

    @staticmethod
    def _cell(
        row: Any,
        column: int,
        text: str,
        *,
        header: bool = False,
        color: str | None = None,
    ) -> None:
        ctk.CTkLabel(
            row,
            text=text,
            font=font(11, "bold") if header else font(11),
            text_color=color or (TEXT_MUTED if header else TEXT),
            anchor="w",
            wraplength=150,
            justify="left",
        ).grid(row=0, column=column, sticky="w", padx=6, pady=7)
