"""
Stage 5 - Inventory history.

A read-only, searchable view of vw_stock_movements, the SQLite view that
unions stock-in and stock-out detail lines. Stock-out quantities are negative.

  * search matches ingredient name, reason, notes, unit and date
  * a type filter shows all / stock-in / stock-out movements
  * "Details" opens the full transaction header + detail line
  * the header Refresh button re-reads everything from SQLite

History is intentionally read-only: correcting a mistake means recording a
new stock-in or stock-out transaction, never editing an old record.
All SQL lives in stock_repository.py.
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
    font,
)
from formatting import format_quantity

HISTORY_WEIGHTS = (2, 1, 2, 2, 1, 2, 1)
HISTORY_MINSIZES = (150, 56, 150, 100, 58, 120, 82)
HISTORY_TITLES = (
    "Date", "Type", "Ingredient", "Quantity", "Unit", "Reason", "Details"
)

FILTER_LABELS = ("All movements", "Stock in", "Stock out")
FILTER_CODES = {"All movements": "ALL", "Stock in": "IN", "Stock out": "OUT"}


class HistoryView(ctk.CTkFrame):
    """Searchable table of every recorded stock movement."""

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

        self.scroller = ctk.CTkScrollableFrame(self, fg_color=CREAM, corner_radius=0)
        self.scroller.pack(fill="both", expand=True)
        self.container = ctk.CTkFrame(self.scroller, fg_color="transparent")
        self.container.pack(fill="both", expand=True, padx=26, pady=22)

        # Static layout is built once so typing in the search box keeps focus.
        self._build_toolbar()
        self.summary_frame = ctk.CTkFrame(self.container, fg_color="transparent")
        self.summary_frame.pack(fill="x", pady=(0, 16))
        self._build_table_shell()
        self._build_footer()
        self._load()

    def refresh(self) -> None:
        """Header Refresh button re-reads the history from SQLite."""
        self._load()

    # ------------------------------------------------------------------
    # Static layout
    # ------------------------------------------------------------------
    def _build_toolbar(self) -> None:
        top = ctk.CTkFrame(self.container, fg_color="transparent")
        top.pack(fill="x", pady=(0, 14))

        left = ctk.CTkFrame(top, fg_color="transparent")
        left.pack(side="left")
        ctk.CTkLabel(
            left, text="Movement history", font=font(20, "bold"), text_color=TEXT
        ).pack(anchor="w")
        self.count_label = ctk.CTkLabel(
            left, text="", font=font(11), text_color=TEXT_MUTED, anchor="w"
        )
        self.count_label.pack(anchor="w")

        right = ctk.CTkFrame(top, fg_color="transparent")
        right.pack(side="right")

        self.clear_button = ctk.CTkButton(
            right,
            text="Clear",
            command=self._clear_filters,
            width=66,
            height=38,
            corner_radius=9,
            fg_color=PANEL,
            hover_color="#F0E9DD",
            border_width=1,
            border_color=BORDER,
            text_color=TEXT_MUTED,
            font=font(12),
        )
        self.clear_button.pack(side="right")

        self.filter_combo = ctk.CTkComboBox(
            right,
            values=list(FILTER_LABELS),
            width=150,
            height=38,
            corner_radius=9,
            fg_color=CARD,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT,
            font=font(12),
            dropdown_font=font(12),
            command=lambda _value: self._load(),
        )
        self.filter_combo.pack(side="right", padx=8)
        self.filter_combo.set(FILTER_LABELS[0])

        self.search_entry = ctk.CTkEntry(
            right,
            placeholder_text="Search ingredient, reason, date…",
            width=250,
            height=38,
            corner_radius=9,
            fg_color=CARD,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT,
            font=font(12),
        )
        self.search_entry.pack(side="right", padx=(0, 8))
        self.search_entry.bind("<KeyRelease>", lambda _event: self._load())

    def _build_table_shell(self) -> None:
        self.panel = ctk.CTkFrame(
            self.container,
            fg_color=CARD,
            corner_radius=14,
            border_width=1,
            border_color=BORDER,
        )
        self.panel.pack(fill="x")

        head = ctk.CTkFrame(self.panel, fg_color="transparent")
        head.pack(fill="x", padx=20, pady=(14, 4))
        ctk.CTkLabel(
            head, text="All stock movements", font=font(14, "bold"),
            text_color=TEXT,
        ).pack(side="left")
        ctk.CTkLabel(
            head,
            text="Newest first  ·  read-only",
            font=font(11),
            text_color=TEXT_MUTED,
        ).pack(side="right")

        header = self._row_frame(self.panel, PANEL)
        header.pack(fill="x", padx=16, pady=(8, 6))
        for column, title in enumerate(HISTORY_TITLES):
            self._cell(header, column, title, header=True)

        self.rows_frame = ctk.CTkFrame(self.panel, fg_color="transparent")
        self.rows_frame.pack(fill="x", padx=16, pady=(0, 8))

        self.message_label = ctk.CTkLabel(
            self.panel,
            text="",
            font=font(11),
            text_color=TEXT_MUTED,
            anchor="w",
            wraplength=800,
            justify="left",
        )
        self.message_label.pack(fill="x", padx=20, pady=(0, 16))

    def _build_footer(self) -> None:
        notes = (
            "Stock-in quantities are positive, stock-out quantities negative. "
            "All rows come from the vw_stock_movements SQLite view.",
            "History is read-only: to correct a mistake, record a new "
            "stock-in or stock-out transaction on the Inventory screen.",
            "Only the newest "
            f"{stock.HISTORY_ROW_LIMIT} matching rows are shown; use the "
            "search box to narrow older records.",
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
            ).pack(anchor="w", pady=(10 if index == 0 else 2,
                                     12 if index == len(notes) - 1 else 0))

    # ------------------------------------------------------------------
    # Data load (only the summary and the table body are rebuilt)
    # ------------------------------------------------------------------
    def _load(self) -> None:
        code = FILTER_CODES.get(self.filter_combo.get(), "ALL")
        keyword = self.search_entry.get()
        try:
            data = stock.get_history(code, keyword)
        except (ValueError, sqlite3.Error) as exc:
            self.message_label.configure(
                text=f"Could not load history: {exc}", text_color=DANGER
            )
            return

        shown = len(data["rows"])
        self.count_label.configure(
            text=(
                f"{shown} shown  ·  {data['filtered']} matching  ·  "
                f"{data['total']} recorded in total"
            )
        )
        self.message_label.configure(
            text=(
                f"Showing {shown} of {data['filtered']} matching movement(s)."
                if data["filtered"] > shown
                else "All matching movements are shown."
            ),
            text_color=TEXT_MUTED,
        )

        self._render_summary(data)
        self._render_rows(data["rows"])

    def _render_summary(self, data: dict[str, Any]) -> None:
        for child in self.summary_frame.winfo_children():
            child.destroy()
        for column in range(4):
            self.summary_frame.grid_columnconfigure(column, weight=1)
        cards = (
            ("Total movements", data["total"], "Stock-in + stock-out lines", ACCENT),
            ("Stock-in lines", data["stock_in_lines"], "Deliveries received", SUCCESS),
            ("Stock-out lines", data["stock_out_lines"], "Stock removed", DANGER),
            ("Last activity", data["last_movement"], "Most recent transaction",
             TEXT_MUTED),
        )
        for column, (label, value, note, accent) in enumerate(cards):
            self._stat_card(column, label, value, note, accent,
                            value_size=17 if column == 3 else 26)

    def _stat_card(
        self,
        column: int,
        label: str,
        value: Any,
        note: str,
        accent: str,
        value_size: int = 26,
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

    def _render_rows(self, rows: list[dict[str, Any]]) -> None:
        for child in self.rows_frame.winfo_children():
            child.destroy()

        if not rows:
            empty = ctk.CTkFrame(self.rows_frame, fg_color=PANEL, corner_radius=10)
            empty.pack(fill="x", pady=(8, 12))
            ctk.CTkLabel(
                empty, text="No movements match", font=font(14, "bold"),
                text_color=TEXT,
            ).pack(pady=(18, 2))
            ctk.CTkLabel(
                empty,
                text=(
                    "Record a stock-in or stock-out on the Inventory screen, "
                    "or clear the search and type filter."
                ),
                font=font(11), text_color=TEXT_MUTED, wraplength=600,
            ).pack(pady=(0, 18))
            return

        for movement in rows:
            self._movement_row(movement)

    @staticmethod
    def _row_frame(parent: Any, background: str) -> ctk.CTkFrame:
        row = ctk.CTkFrame(parent, fg_color=background, corner_radius=8)
        for index, weight in enumerate(HISTORY_WEIGHTS):
            row.grid_columnconfigure(
                index,
                weight=weight,
                minsize=HISTORY_MINSIZES[index],
                uniform="history_columns",
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
            wraplength=145,
            justify="left",
        ).grid(row=0, column=column, sticky="w", padx=6, pady=7)

    def _movement_row(self, movement: dict[str, Any]) -> None:
        row = self._row_frame(self.rows_frame, CARD)
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
            format_quantity(movement["quantity"], "", signed=True),
            color=SUCCESS if kind == "IN" else DANGER,
        )
        self._cell(row, 4, str(movement["unit"]))
        self._cell(row, 5, str(movement["reason"] or "—"))

        ctk.CTkButton(
            row,
            text="Details",
            command=lambda current=movement: self._open_detail(current),
            width=70,
            height=26,
            corner_radius=7,
            fg_color=PANEL,
            hover_color="#F0E9DD",
            border_width=1,
            border_color=BORDER,
            text_color=TEXT,
            font=font(11),
        ).grid(row=0, column=6, sticky="w", padx=6, pady=5)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def _clear_filters(self) -> None:
        self.search_entry.delete(0, "end")
        self.filter_combo.set(FILTER_LABELS[0])
        self._load()
        self.search_entry.focus_set()

    def _open_detail(self, movement: dict[str, Any]) -> None:
        try:
            detail = stock.get_movement_detail(
                movement["movement_type"],
                movement["transaction_id"],
                movement["detail_id"],
            )
        except (ValueError, sqlite3.Error) as exc:
            self.message_label.configure(
                text=f"Could not open details: {exc}", text_color=DANGER
            )
            return
        self.dialog = MovementDetailDialog(self, detail)


class MovementDetailDialog(ModalDialog):
    """Read-only full record for one movement line."""

    def __init__(self, master: Any, detail: dict[str, Any]) -> None:
        super().__init__(master, "Movement details", 470)
        kind = detail["movement_type"]
        self.heading(
            "Stock in" if kind == "IN" else "Stock out",
            f"Transaction #{detail['transaction_id']}  ·  "
            f"line #{detail['detail_id']}  ·  {detail['transaction_date']}",
        )

        panel = ctk.CTkFrame(
            self.card,
            fg_color=PANEL,
            corner_radius=10,
            border_width=1,
            border_color=BORDER,
        )
        panel.pack(fill="x", padx=24)

        fields: list[tuple[str, str]] = [
            ("Ingredient", str(detail["ingredient_name"])),
            (
                "Quantity",
                format_quantity(detail["quantity"], detail["unit"], signed=True),
            ),
            (
                "Reference" if kind == "IN" else "Reason",
                str(detail["reference_no"] or "—")
                if kind == "IN" else str(detail["reason"]),
            ),
            ("Recorded by", str(detail["created_by"] or "—")),
            ("Transaction notes", str(detail["notes"] or "—")),
            ("Line notes", str(detail["line_notes"] or "—")),
        ]
        if kind == "IN":
            fields.append(
                ("Delivery expiry", str(detail["expiration_date"] or "—"))
            )

        for index, (label, value) in enumerate(fields):
            line = ctk.CTkFrame(panel, fg_color="transparent")
            # An empty packed spacer draws a faint seam across a card on
            # Windows, so the bottom padding lives on the last row instead.
            bottom = 12 if index == len(fields) - 1 else 0
            line.pack(fill="x", padx=14, pady=(10 if index == 0 else 4, bottom))
            ctk.CTkLabel(
                line, text=label, font=font(11, "bold"), text_color=TEXT_MUTED,
                width=130, anchor="w",
            ).pack(side="left")
            ctk.CTkLabel(
                line, text=value, font=font(12), text_color=TEXT,
                anchor="w", wraplength=250, justify="left",
            ).pack(side="left")

        ctk.CTkButton(
            self.card,
            text="Close",
            command=self.close,
            height=38,
            corner_radius=10,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            text_color="#FFFFFF",
            font=font(13, "bold"),
        ).pack(fill="x", padx=24, pady=(16, 22))
        self.finish()
