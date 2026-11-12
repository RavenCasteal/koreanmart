"""
Stage 5 - Inventory movements (stock-in / stock-out).

What this screen does:
  * shows the LIVE quantity_on_hand of every active ingredient
  * records a stock-in delivery: one transaction header + several detail lines
  * records a stock-out removal: one transaction header + several detail lines,
    with a required reason and a check that no balance can go negative
  * shows the latest movements and links to the full History screen
  * reads every figure from SQLite; the header Refresh button re-reads it all

What it deliberately does NOT do:
  * let anyone type a balance directly. Only a stock-in / stock-out
    transaction may change quantity_on_hand (enforced in stock_repository.py).
  * touch recipe_ingredients or recipes. Recipe quantities and physical stock
    remain separate.
  * calculate servings or deduct stock automatically - the Calculator screen
    only reports what is needed; it never writes to the database.

All SQL lives in stock_repository.py; this file only builds the interface.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable

import customtkinter as ctk

import stock_repository as stock
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
from formatting import format_number, format_quantity

# Every row is its own grid: identical minsize + uniform group keeps the
# header and the data columns aligned when the window is resized.
BALANCE_WEIGHTS = (3, 1, 2, 2, 2, 2)
BALANCE_MINSIZES = (170, 60, 110, 100, 108, 158)
BALANCE_TITLES = (
    "Ingredient", "Unit", "On hand", "Minimum", "Status", "Actions"
)

RECENT_WEIGHTS = (2, 1, 2, 2, 2)
RECENT_MINSIZES = (160, 60, 150, 110, 130)
RECENT_TITLES = ("Date", "Type", "Ingredient", "Quantity", "Reason")


class InventoryView(ctk.CTkFrame):
    """Current balances plus the forms that create stock movements."""

    def __init__(
        self,
        master: Any,
        user: dict[str, Any] | None = None,
        on_navigate: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(master, fg_color=CREAM, corner_radius=0)
        self.user = user
        self.on_navigate = on_navigate
        self.dialog: ModalDialog | None = None
        self._message = ""
        self._message_ok = True

        self.scroller = ctk.CTkScrollableFrame(self, fg_color=CREAM, corner_radius=0)
        self.scroller.pack(fill="both", expand=True)
        self.container = ctk.CTkFrame(self.scroller, fg_color="transparent")
        self.container.pack(fill="both", expand=True, padx=26, pady=22)
        self._render()

    def refresh(self) -> None:
        """Header Refresh button re-reads balances and movements from SQLite."""
        self._render()

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    def _clear(self) -> None:
        for child in self.container.winfo_children():
            child.destroy()

    def _render(self) -> None:
        self._clear()
        try:
            summary = stock.get_inventory_summary()
            balances = stock.list_stock_ingredients()
            recent = stock.get_recent_movements(5)
        except (ValueError, sqlite3.Error) as exc:
            ctk.CTkLabel(
                self.container,
                text=f"Could not load inventory: {exc}",
                font=font(13),
                text_color=DANGER,
                anchor="w",
            ).pack(anchor="w")
            return

        self._build_toolbar(summary)
        self._build_summary(summary)
        self._build_balances(balances)
        self._build_recent(recent)
        self._build_message()
        self._build_footer()

    def _build_toolbar(self, summary: dict[str, Any]) -> None:
        top = ctk.CTkFrame(self.container, fg_color="transparent")
        top.pack(fill="x", pady=(0, 14))

        left = ctk.CTkFrame(top, fg_color="transparent")
        left.pack(side="left")
        ctk.CTkLabel(
            left, text="Current stock", font=font(20, "bold"), text_color=TEXT
        ).pack(anchor="w")
        ctk.CTkLabel(
            left,
            text=(
                f"{summary['total_movements']} movement line(s) recorded  ·  "
                f"last activity {summary['last_movement']}"
            ),
            font=font(11),
            text_color=TEXT_MUTED,
            anchor="w",
        ).pack(anchor="w")

        ctk.CTkButton(
            top,
            text="+  Stock in",
            command=self._open_stock_in,
            width=130,
            height=38,
            corner_radius=9,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            text_color="#FFFFFF",
            font=font(12, "bold"),
        ).pack(side="right", padx=(8, 0))
        ctk.CTkButton(
            top,
            text="−  Stock out",
            command=self._open_stock_out,
            width=130,
            height=38,
            corner_radius=9,
            fg_color=PANEL,
            hover_color="#F0E9DD",
            border_width=1,
            border_color=BORDER,
            text_color=TEXT,
            font=font(12, "bold"),
        ).pack(side="right")

    def _build_summary(self, summary: dict[str, Any]) -> None:
        cards = ctk.CTkFrame(self.container, fg_color="transparent")
        cards.pack(fill="x", pady=(0, 16))
        for column in range(4):
            cards.grid_columnconfigure(column, weight=1)

        self._stat_card(
            cards, 0, "Active ingredients", summary["active_ingredients"],
            "Registered in the catalog", ACCENT,
        )
        self._stat_card(
            cards, 1, "With stock", summary["with_stock"],
            "Balance is above zero", SUCCESS,
        )
        self._stat_card(
            cards, 2, "Need attention",
            summary["low_stock"] + summary["out_of_stock"],
            "Low stock + out of stock", WARNING,
        )
        self._stat_card(
            cards, 3, "Stock-in / out lines",
            f"{summary['stock_in_lines']} / {summary['stock_out_lines']}",
            "Recorded transaction details", TEXT_MUTED, value_size=22,
        )

    @staticmethod
    def _stat_card(
        parent: Any,
        column: int,
        label: str,
        value: Any,
        note: str,
        accent: str,
        value_size: int = 26,
    ) -> None:
        card = ctk.CTkFrame(
            parent,
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
            font=font(value_size, "bold"),
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
    # Balance table
    # ------------------------------------------------------------------
    def _build_balances(self, balances: list[dict[str, Any]]) -> None:
        panel = ctk.CTkFrame(
            self.container,
            fg_color=CARD,
            corner_radius=14,
            border_width=1,
            border_color=BORDER,
        )
        panel.pack(fill="x", pady=(0, 16))

        head = ctk.CTkFrame(panel, fg_color="transparent")
        head.pack(fill="x", padx=20, pady=(14, 4))
        ctk.CTkLabel(
            head, text="Ingredient balances", font=font(14, "bold"),
            text_color=TEXT,
        ).pack(side="left")
        ctk.CTkLabel(
            head,
            text=f"{len(balances)} active ingredient(s)  ·  read from SQLite",
            font=font(11),
            text_color=TEXT_MUTED,
        ).pack(side="right")

        header = self._row_frame(panel, PANEL, BALANCE_WEIGHTS,
                                 BALANCE_MINSIZES, "balance_columns")
        header.pack(fill="x", padx=16, pady=(8, 6))
        for column, title in enumerate(BALANCE_TITLES):
            self._cell(header, column, title, header=True)

        body = ctk.CTkFrame(panel, fg_color="transparent")
        body.pack(fill="x", padx=16, pady=(0, 8))
        if not balances:
            empty = ctk.CTkFrame(body, fg_color=PANEL, corner_radius=10)
            empty.pack(fill="x", pady=(8, 10))
            ctk.CTkLabel(
                empty, text="No active ingredients yet",
                font=font(14, "bold"), text_color=TEXT,
            ).pack(pady=(18, 2))
            ctk.CTkLabel(
                empty,
                text="Add one on the Ingredients screen before recording stock.",
                font=font(11), text_color=TEXT_MUTED,
            ).pack(pady=(0, 18))
        else:
            for item in balances:
                self._balance_row(body, item)

        ctk.CTkLabel(
            panel,
            text=(
                "Balances change only when a stock-in or stock-out transaction "
                "is saved. Quick actions pre-select that ingredient in the form."
            ),
            font=font(11),
            text_color=TEXT_MUTED,
            anchor="w",
            wraplength=800,
            justify="left",
        ).pack(fill="x", padx=20, pady=(0, 16))

    @staticmethod
    def _row_frame(
        parent: Any,
        background: str,
        weights: tuple[int, ...],
        minsizes: tuple[int, ...],
        group: str,
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
            font=font(11, "bold") if header else font(12),
            text_color=color or (TEXT_MUTED if header else TEXT),
            anchor="w",
            wraplength=160,
            justify="left",
        ).grid(row=0, column=column, sticky="w", padx=6, pady=7)

    def _balance_row(self, parent: Any, item: dict[str, Any]) -> None:
        row = self._row_frame(
            parent, CARD, BALANCE_WEIGHTS, BALANCE_MINSIZES, "balance_columns"
        )
        row.pack(fill="x", pady=2)

        self._cell(row, 0, str(item["name"]))
        self._cell(row, 1, str(item["unit"]))
        self._cell(
            row, 2,
            format_quantity(item["quantity_on_hand"], item["unit"]),
            color=SUCCESS if item["quantity_on_hand"] > 0 else DANGER,
        )
        self._cell(row, 3, format_number(item["minimum_stock"]))

        status = item["status"]
        colour = (
            DANGER if status == "Out of stock"
            else WARNING if status == "Low stock"
            else SUCCESS
        )
        ctk.CTkLabel(
            row,
            text=status,
            fg_color=colour,
            text_color="#FFFFFF",
            corner_radius=6,
            width=94,
            height=22,
            font=font(11, "bold"),
        ).grid(row=0, column=4, sticky="w", padx=6, pady=7)

        ingredient_id = int(item["ingredient_id"])
        actions = ctk.CTkFrame(row, fg_color="transparent")
        actions.grid(row=0, column=5, sticky="w", padx=6, pady=5)
        ctk.CTkButton(
            actions,
            text="In",
            command=lambda value=ingredient_id: self._open_stock_in(value),
            width=40,
            height=26,
            corner_radius=7,
            fg_color=PANEL,
            hover_color="#F0E9DD",
            border_width=1,
            border_color=BORDER,
            text_color=SUCCESS,
            font=font(11, "bold"),
        ).pack(side="left")
        ctk.CTkButton(
            actions,
            text="Out",
            command=lambda value=ingredient_id: self._open_stock_out(value),
            width=40,
            height=26,
            corner_radius=7,
            fg_color=PANEL,
            hover_color="#F8E5E1",
            border_width=1,
            border_color=BORDER,
            text_color=DANGER,
            font=font(11, "bold"),
        ).pack(side="left", padx=6)
        ctk.CTkButton(
            actions,
            text="History",
            command=self._go_history,
            width=56,
            height=26,
            corner_radius=7,
            fg_color="transparent",
            hover_color="#F0E9DD",
            border_width=1,
            border_color=BORDER,
            text_color=TEXT_MUTED,
            font=font(11),
        ).pack(side="left", padx=(0, 0))

    # ------------------------------------------------------------------
    # Recent movements
    # ------------------------------------------------------------------
    def _build_recent(self, recent: list[dict[str, Any]]) -> None:
        panel = ctk.CTkFrame(
            self.container,
            fg_color=CARD,
            corner_radius=14,
            border_width=1,
            border_color=BORDER,
        )
        panel.pack(fill="x")

        head = ctk.CTkFrame(panel, fg_color="transparent")
        head.pack(fill="x", padx=20, pady=(14, 4))
        ctk.CTkLabel(
            head, text="Latest movements", font=font(14, "bold"), text_color=TEXT
        ).pack(side="left")
        ctk.CTkButton(
            head,
            text="View all history  →",
            command=self._go_history,
            width=142,
            height=28,
            corner_radius=7,
            fg_color=PANEL,
            hover_color="#F0E9DD",
            border_width=1,
            border_color=BORDER,
            text_color=TEXT_MUTED,
            font=font(11),
        ).pack(side="right")

        header = self._row_frame(panel, PANEL, RECENT_WEIGHTS,
                                 RECENT_MINSIZES, "recent_columns")
        header.pack(fill="x", padx=16, pady=(8, 6))
        for column, title in enumerate(RECENT_TITLES):
            self._cell(header, column, title, header=True)

        body = ctk.CTkFrame(panel, fg_color="transparent")
        body.pack(fill="x", padx=16, pady=(0, 8))
        if not recent:
            empty = ctk.CTkFrame(body, fg_color=PANEL, corner_radius=10)
            empty.pack(fill="x", pady=(8, 10))
            ctk.CTkLabel(
                empty, text="No stock movements yet",
                font=font(14, "bold"), text_color=TEXT,
            ).pack(pady=(16, 2))
            ctk.CTkLabel(
                empty,
                text="Record a delivery to fill the balances and the history.",
                font=font(11), text_color=TEXT_MUTED,
            ).pack(pady=(0, 8))
            ctk.CTkButton(
                empty,
                text="Record stock-in",
                command=self._open_stock_in,
                width=150,
                height=32,
                corner_radius=8,
                fg_color=ACCENT,
                hover_color=ACCENT_HOVER,
                text_color="#FFFFFF",
                font=font(11, "bold"),
            ).pack(pady=(0, 16))
        else:
            for movement in recent:
                row = self._row_frame(
                    body, CARD, RECENT_WEIGHTS, RECENT_MINSIZES, "recent_columns"
                )
                row.pack(fill="x", pady=2)
                kind = movement["movement_type"]
                self._cell(row, 0, str(movement["transaction_date"]))
                ctk.CTkLabel(
                    row,
                    text=kind,
                    fg_color=SUCCESS if kind == "IN" else ACCENT,
                    text_color="#FFFFFF",
                    corner_radius=6,
                    width=42,
                    height=22,
                    font=font(11, "bold"),
                ).grid(row=0, column=1, sticky="w", padx=6, pady=7)
                self._cell(row, 2, str(movement["ingredient_name"]))
                self._cell(
                    row, 3,
                    format_quantity(
                        movement["quantity"], movement["unit"], signed=True
                    ),
                    color=SUCCESS if kind == "IN" else DANGER,
                )
                self._cell(row, 4, str(movement["reason"] or "—"))

        ctk.CTkLabel(
            panel,
            text="Stock-out quantities are shown with a minus sign.",
            font=font(11),
            text_color=TEXT_MUTED,
            anchor="w",
        ).pack(fill="x", padx=20, pady=(2, 16))

    # ------------------------------------------------------------------
    # Messages / footer
    # ------------------------------------------------------------------
    def _build_message(self) -> None:
        self.message_label = ctk.CTkLabel(
            self.container,
            text="",
            font=font(12, "bold"),
            text_color=SUCCESS,
            anchor="w",
            wraplength=820,
            justify="left",
            height=24,
        )
        self.message_label.pack(fill="x", pady=(10, 0))
        self._apply_message()

    def _build_footer(self) -> None:
        notes = (
            "A transaction saves a header plus one or more ingredient lines in "
            "ONE database transaction - either everything is written or nothing is.",
            "Stock-out is refused when a line would make any balance negative. "
            "No balance can ever be typed directly into a form.",
            "Recipe quantities are untouched by stock movements. The Calculator "
            "screen checks servings and never deducts stock.",
        )
        for index, text in enumerate(notes):
            ctk.CTkLabel(
                self.container,
                text="·  " + text,
                font=font(11),
                text_color=TEXT_MUTED,
                anchor="w",
                wraplength=820,
                justify="left",
            ).pack(anchor="w", pady=(2, 12 if index == len(notes) - 1 else 0))

    def _apply_message(self) -> None:
        label = getattr(self, "message_label", None)
        if label is not None:
            label.configure(
                text=self._message,
                text_color=SUCCESS if self._message_ok else DANGER,
            )

    def _set_message(self, text: str, ok: bool = True) -> None:
        self._message = text
        self._message_ok = ok
        self._apply_message()

    def _after_save(self, message: str) -> None:
        self._set_message(message)
        self.refresh()

    def _go_history(self) -> None:
        if self.on_navigate is not None:
            self.on_navigate("history")

    # ------------------------------------------------------------------
    # Dialogs
    # ------------------------------------------------------------------
    def _ingredients_for_form(self) -> list[dict[str, Any]] | None:
        try:
            ingredients = stock.list_stock_ingredients()
        except sqlite3.Error as exc:
            self._set_message(f"Could not load ingredients: {exc}", ok=False)
            return None
        if not ingredients:
            self._set_message(
                "Add an active ingredient on the Ingredients screen first.",
                ok=False,
            )
            return None
        return ingredients

    def _open_stock_in(self, preselect_id: int | None = None) -> None:
        self._set_message("")
        ingredients = self._ingredients_for_form()
        if ingredients is None:
            return
        self.dialog = StockInDialog(
            self,
            ingredients=ingredients,
            user_id=(self.user or {}).get("user_id"),
            on_saved=self._after_save,
            preselect_id=preselect_id,
        )

    def _open_stock_out(self, preselect_id: int | None = None) -> None:
        self._set_message("")
        ingredients = self._ingredients_for_form()
        if ingredients is None:
            return
        self.dialog = StockOutDialog(
            self,
            ingredients=ingredients,
            user_id=(self.user or {}).get("user_id"),
            on_saved=self._after_save,
            preselect_id=preselect_id,
        )


# ----------------------------------------------------------------------
# Multi-line transaction dialogs
# ----------------------------------------------------------------------
class TransactionDialog(ModalDialog):
    """Shared multi-line form for stock-in and stock-out transactions.

    A subclass supplies the header field (reference number / removal reason)
    and the repository call. This base owns the repeatable ingredient lines,
    validation feedback and the save flow.
    """

    WIDTH = 720
    HEIGHT = 540
    TITLE = "Stock transaction"
    HEADING = "Record a transaction"
    SUBTITLE = "One transaction may contain several ingredient lines."
    SAVE_TEXT = "Save transaction"
    WITH_EXPIRY = False

    def __init__(
        self,
        master: Any,
        ingredients: list[dict[str, Any]],
        user_id: Any,
        on_saved: Callable[[str], None] | None = None,
        preselect_id: int | None = None,
    ) -> None:
        super().__init__(master, self.TITLE, self.WIDTH)
        # One rounded card: a scrollable body for the fields plus a FIXED
        # footer for the error message and the Save/Cancel buttons. The form
        # can grow to many lines without pushing the buttons off screen.
        self.card.destroy()
        self.outer_card = ctk.CTkFrame(
            self,
            fg_color=CARD,
            corner_radius=18,
            border_width=1,
            border_color=BORDER,
        )
        self.outer_card.pack(fill="both", expand=True, padx=20, pady=20)

        self.footer = ctk.CTkFrame(self.outer_card, fg_color="transparent")
        self.footer.pack(side="bottom", fill="x")

        self.scroller = ctk.CTkScrollableFrame(
            self.outer_card, fg_color=CARD, corner_radius=0
        )
        self.scroller.pack(side="top", fill="both", expand=True, padx=20)
        self.card = ctk.CTkFrame(self.scroller, fg_color="transparent")
        self.card.pack(fill="both", expand=True, pady=(20, 4))

        self.ingredients = ingredients
        self.user_id = user_id
        self.on_saved = on_saved
        self.line_rows: list[dict[str, Any]] = []

        # The label carries the current balance so the user can see what is
        # available without leaving the form.
        self.labels: dict[str, int] = {}
        for item in ingredients:
            label = (
                f"{item['name']} ({item['unit']})  ·  "
                f"{format_number(item['quantity_on_hand'])} on hand"
            )
            self.labels[label] = int(item["ingredient_id"])
        self.label_list = list(self.labels)

        self.heading(self.HEADING, self.SUBTITLE)
        self._build_header_fields()
        self._build_lines_section()
        self.notes_entry = self.field("Transaction notes (optional)")
        self.error_label(height=34)
        self.actions(self.SAVE_TEXT, self._save)
        self._add_line(preselect_id)
        self.finish()

    def error_label(self, height: int = 32) -> ctk.CTkLabel:
        """Validation message lives in the fixed footer, always visible."""
        self.error_message = ctk.CTkLabel(
            self.footer,
            text="",
            font=font(11),
            text_color=DANGER,
            wraplength=self._width - 90,
            justify="left",
            anchor="w",
            height=height,
        )
        self.error_message.pack(fill="x", padx=24, pady=(6, 0))
        return self.error_message

    def actions(
        self,
        save_text: str,
        on_save: Callable[[], None],
        cancel_text: str = "Cancel",
        danger: bool = False,
    ) -> None:
        """Save + Cancel are pinned to the footer, outside the scroll area."""
        ctk.CTkButton(
            self.footer,
            text=save_text,
            command=on_save,
            height=42,
            corner_radius=10,
            fg_color=DANGER if danger else ACCENT,
            hover_color="#8F2D21" if danger else ACCENT_HOVER,
            text_color="#FFFFFF",
            font=font(13, "bold"),
        ).pack(fill="x", padx=24, pady=(6, 6))
        ctk.CTkButton(
            self.footer,
            text=cancel_text,
            command=self.close,
            height=32,
            corner_radius=10,
            fg_color="transparent",
            hover_color=PANEL,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT_MUTED,
            font=font(12),
        ).pack(fill="x", padx=24, pady=(0, 18))

    def center(self) -> None:
        """Fixed, screen-friendly size; extra lines scroll inside the card."""
        self.update_idletasks()
        root = self.master.winfo_toplevel()
        height = min(self.HEIGHT, max(360, root.winfo_screenheight() - 100))
        width = self._width
        x = root.winfo_x() + (root.winfo_width() - width) // 2
        y = root.winfo_y() + (root.winfo_height() - height) // 2
        self.geometry(f"{width}x{height}+{max(x, 0)}+{max(y, 0)}")

    # -- subclass hooks ------------------------------------------------
    def _build_header_fields(self) -> None:
        raise NotImplementedError

    def _header_values(self) -> dict[str, Any]:
        return {
            "reference_no": "",
            "reason": "",
            "notes": self.notes_entry.get(),
        }

    def _submit(self, lines: list[dict[str, Any]], header: dict[str, Any]) -> dict:
        raise NotImplementedError

    def _success_message(self, result: dict[str, Any]) -> str:
        raise NotImplementedError

    # -- shared line editing -------------------------------------------
    def _build_lines_section(self) -> None:
        ctk.CTkLabel(
            self.card, text="Ingredient lines",
            font=font(12, "bold"), text_color=TEXT,
        ).pack(anchor="w", padx=24, pady=(14, 4))
        # The whole card scrolls, so the line area can grow naturally here.
        self.lines_frame = ctk.CTkFrame(self.card, fg_color="transparent")
        self.lines_frame.pack(fill="x", padx=24)
        ctk.CTkButton(
            self.card,
            text="+  Add another ingredient",
            command=self._add_line,
            height=32,
            corner_radius=8,
            fg_color=PANEL,
            hover_color="#F0E9DD",
            border_width=1,
            border_color=BORDER,
            text_color=TEXT_MUTED,
            font=font(11),
        ).pack(fill="x", padx=24, pady=(8, 0))

    def _add_line(self, preselect_id: int | None = None) -> None:
        maximum = min(stock.MAX_LINES_PER_TRANSACTION, len(self.label_list))
        if len(self.line_rows) >= maximum:
            self.show_error(
                "Every active ingredient already has a line. "
                "Save this transaction and start another one."
            )
            return
        self.show_error("")

        frame = ctk.CTkFrame(
            self.lines_frame,
            fg_color=CARD,
            corner_radius=8,
            border_width=1,
            border_color=BORDER,
        )
        frame.pack(fill="x", pady=3)

        combo = ctk.CTkComboBox(
            frame,
            values=self.label_list,
            width=330,
            height=34,
            corner_radius=8,
            fg_color=CARD,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT,
            font=font(12),
            dropdown_font=font(11),
        )
        combo.pack(side="left", padx=(8, 6), pady=8)

        quantity = ctk.CTkEntry(
            frame,
            width=86,
            height=34,
            corner_radius=8,
            placeholder_text="Qty",
            fg_color=CARD,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT,
            font=font(12),
        )
        quantity.pack(side="left", padx=4)

        expiry = None
        if self.WITH_EXPIRY:
            expiry = ctk.CTkEntry(
                frame,
                width=120,
                height=34,
                corner_radius=8,
                placeholder_text="YYYY-MM-DD",
                fg_color=CARD,
                border_width=1,
                border_color=BORDER,
                text_color=TEXT,
                font=font(12),
            )
            expiry.pack(side="left", padx=4)

        row: dict[str, Any] = {
            "frame": frame,
            "combo": combo,
            "quantity": quantity,
            "expiry": expiry,
        }
        remove = ctk.CTkButton(
            frame,
            text="×",
            command=lambda current=row: self._remove_line(current),
            width=28,
            height=28,
            corner_radius=7,
            fg_color="transparent",
            hover_color="#F8E5E1",
            border_width=1,
            border_color=BORDER,
            text_color=DANGER,
            font=font(14, "bold"),
        )
        remove.pack(side="left", padx=(6, 8))
        self.line_rows.append(row)

        # Prefer a label not already used, so two lines rarely collide.
        used = {item["combo"].get() for item in self.line_rows[:-1]}
        default = next(
            (label for label in self.label_list if label not in used),
            self.label_list[0],
        )
        if preselect_id is not None:
            for label, ingredient_id in self.labels.items():
                if ingredient_id == preselect_id:
                    default = label
                    break
        combo.set(default)
        self.center()

    def _remove_line(self, row: dict[str, Any]) -> None:
        if len(self.line_rows) <= 1:
            self.show_error("A transaction needs at least one ingredient line.")
            return
        self.show_error("")
        row["frame"].destroy()
        self.line_rows.remove(row)
        self.center()

    def _collect_lines(self) -> list[dict[str, Any]]:
        lines: list[dict[str, Any]] = []
        for index, row in enumerate(self.line_rows, start=1):
            ingredient_id = self.labels.get(row["combo"].get())
            if ingredient_id is None:
                raise ValueError(
                    f"Line {index}: choose an ingredient from the list."
                )
            lines.append(
                {
                    "ingredient_id": ingredient_id,
                    "quantity": row["quantity"].get(),
                    "expiration_date": (
                        row["expiry"].get() if row["expiry"] is not None else ""
                    ),
                    "notes": "",
                }
            )
        return lines

    def _save(self) -> None:
        self.show_error("")
        try:
            lines = self._collect_lines()
            header = self._header_values()
            result = self._submit(lines, header)
        except (ValueError, sqlite3.Error) as exc:
            self.show_error(str(exc))
            return
        self.close()
        if self.on_saved:
            self.on_saved(self._success_message(result))


class StockInDialog(TransactionDialog):
    """Delivery form: reference number, quantities and optional expiry dates."""

    TITLE = "Record stock-in"
    HEADING = "Record a delivery"
    SUBTITLE = (
        "Each line increases that ingredient's balance. All lines are saved "
        "as one transaction in SQLite."
    )
    SAVE_TEXT = "Save stock-in"
    WITH_EXPIRY = True

    def _build_header_fields(self) -> None:
        self.reference_entry = self.field(
            "Reference number (optional, e.g. PO-1042)"
        )

    def _header_values(self) -> dict[str, Any]:
        values = super()._header_values()
        values["reference_no"] = self.reference_entry.get()
        return values

    def _submit(self, lines: list[dict[str, Any]], header: dict[str, Any]) -> dict:
        return stock.record_stock_in(
            lines,
            reference_no=header["reference_no"],
            notes=header["notes"],
            user_id=self.user_id,
        )

    def _success_message(self, result: dict[str, Any]) -> str:
        first = result["lines"][0]
        return (
            f"Stock-in #{result['transaction_id']} saved: "
            f"{result['line_count']} line(s). "
            f"{first['name']} now "
            f"{format_quantity(first['new_balance'], first['unit'])}."
        )


class StockOutDialog(TransactionDialog):
    """Removal form: required reason, quantities limited by current balances."""

    TITLE = "Record stock-out"
    HEADING = "Remove stock"
    SUBTITLE = (
        "Each line decreases that ingredient's balance. A line that would "
        "make stock negative is refused before anything is saved."
    )
    SAVE_TEXT = "Save stock-out"
    WITH_EXPIRY = False

    def _build_header_fields(self) -> None:
        ctk.CTkLabel(
            self.card, text="Reason for removal",
            font=font(12, "bold"), text_color=TEXT,
        ).pack(anchor="w", padx=24, pady=(12, 4))
        self.reason_combo = ctk.CTkComboBox(
            self.card,
            values=list(stock.STOCK_OUT_REASONS),
            height=40,
            corner_radius=10,
            fg_color=PANEL,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT,
            font=font(13),
            dropdown_font=font(12),
        )
        self.reason_combo.pack(fill="x", padx=24)
        self.reason_combo.set(stock.STOCK_OUT_REASONS[0])

    def _header_values(self) -> dict[str, Any]:
        values = super()._header_values()
        values["reason"] = self.reason_combo.get()
        return values

    def _submit(self, lines: list[dict[str, Any]], header: dict[str, Any]) -> dict:
        return stock.record_stock_out(
            lines,
            reason=header["reason"],
            notes=header["notes"],
            user_id=self.user_id,
        )

    def _success_message(self, result: dict[str, Any]) -> str:
        first = result["lines"][0]
        return (
            f"Stock-out #{result['transaction_id']} saved: "
            f"{result['line_count']} line(s). "
            f"{first['name']} now "
            f"{format_quantity(first['new_balance'], first['unit'])}."
        )
