"""
Stage 6 - ingredient usage calculator.

Answers "can we make N servings of Regular Ramen with the stock we have?"
and shows exactly how much of each ingredient is required, how much is on
hand, and how much is missing.

CRITICAL RULE: this screen is a WHAT-IF view. It performs reads only.
Nothing is deducted from stock, no stock-out transaction is created, and the
recipe is never modified. To actually consume ingredients the user must
record a stock-out on the Inventory screen.

All SQL lives in usage_repository.py; this file only formats the result.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable

import customtkinter as ctk

import usage_repository as usage
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
# header and all data columns aligned when the window is resized.
RESULT_WEIGHTS = (3, 1, 2, 2, 2, 2, 2)
RESULT_MINSIZES = (150, 58, 105, 105, 105, 105, 104)
RESULT_TITLES = (
    "Ingredient", "Unit", "Per serving", "Required", "On hand",
    "Short by", "Status",
)

PRESET_SERVINGS = (1, 5, 10, 25)

VERDICT_OK_BG = "#EAF4EE"
VERDICT_BAD_BG = "#FBEAE6"


class CalculatorView(ctk.CTkFrame):
    """Servings input plus a live, read-only sufficiency breakdown."""

    def __init__(
        self,
        master: Any,
        user: dict[str, Any] | None = None,
        on_navigate: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(master, fg_color=CREAM, corner_radius=0)
        self.user = user
        self.on_navigate = on_navigate
        self.last_result: dict[str, Any] | None = None

        self.scroller = ctk.CTkScrollableFrame(self, fg_color=CREAM, corner_radius=0)
        self.scroller.pack(fill="both", expand=True)
        self.container = ctk.CTkFrame(self.scroller, fg_color="transparent")
        self.container.pack(fill="both", expand=True, padx=26, pady=22)

        # Static layout is built once so typing in the servings box keeps focus.
        self._build_toolbar()
        self._build_input_card()
        self._build_verdict()
        self.summary_frame = ctk.CTkFrame(self.container, fg_color="transparent")
        self.summary_frame.pack(fill="x", pady=(0, 16))
        self._build_results_table()
        self._build_actions()
        self._build_footer()
        self._calculate()

    def refresh(self) -> None:
        """Header Refresh re-reads the recipe and balances from SQLite."""
        self._calculate()

    # ------------------------------------------------------------------
    # Static layout
    # ------------------------------------------------------------------
    def _build_toolbar(self) -> None:
        top = ctk.CTkFrame(self.container, fg_color="transparent")
        top.pack(fill="x", pady=(0, 14))
        ctk.CTkLabel(
            top, text="Usage calculator", font=font(20, "bold"), text_color=TEXT
        ).pack(anchor="w")
        self.context_label = ctk.CTkLabel(
            top, text="", font=font(11), text_color=TEXT_MUTED, anchor="w"
        )
        self.context_label.pack(anchor="w")

    def _build_input_card(self) -> None:
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
            text="How many servings of Regular Ramen?",
            font=font(14, "bold"),
            text_color=TEXT,
        ).pack(anchor="w", padx=20, pady=(18, 2))
        ctk.CTkLabel(
            card,
            text=(
                "Enter a whole number. The calculation only reads your recipe "
                "and current balances."
            ),
            font=font(11),
            text_color=TEXT_MUTED,
            anchor="w",
        ).pack(anchor="w", padx=20)

        form = ctk.CTkFrame(card, fg_color="transparent")
        form.pack(fill="x", padx=20, pady=(14, 0))
        self.servings_entry = ctk.CTkEntry(
            form,
            width=120,
            height=42,
            corner_radius=10,
            fg_color=PANEL,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT,
            font=font(15, "bold"),
            justify="center",
        )
        self.servings_entry.pack(side="left")
        self.servings_entry.insert(0, "1")
        self.servings_entry.bind("<Return>", lambda _event: self._calculate())

        ctk.CTkButton(
            form,
            text="Calculate",
            command=self._calculate,
            width=140,
            height=42,
            corner_radius=10,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            text_color="#FFFFFF",
            font=font(13, "bold"),
        ).pack(side="left", padx=10)

        for value in PRESET_SERVINGS:
            ctk.CTkButton(
                form,
                text=str(value),
                command=lambda count=value: self._use_preset(count),
                width=46,
                height=34,
                corner_radius=8,
                fg_color=PANEL,
                hover_color="#F0E9DD",
                border_width=1,
                border_color=BORDER,
                text_color=TEXT_MUTED,
                font=font(12),
            ).pack(side="left", padx=(0, 6))

        self.error_message = ctk.CTkLabel(
            card,
            text="",
            font=font(11),
            text_color=DANGER,
            anchor="w",
            wraplength=820,
            justify="left",
            height=22,
        )
        self.error_message.pack(fill="x", padx=20, pady=(8, 14))

    def _build_verdict(self) -> None:
        self.verdict = ctk.CTkFrame(
            self.container,
            fg_color=VERDICT_BAD_BG,
            corner_radius=14,
            border_width=1,
            border_color=BORDER,
        )
        self.verdict.pack(fill="x", pady=(0, 16))
        self.verdict_title = ctk.CTkLabel(
            self.verdict, text="", font=font(16, "bold"),
            text_color=DANGER, anchor="w", wraplength=820, justify="left",
        )
        self.verdict_title.pack(anchor="w", padx=20, pady=(16, 2))
        self.verdict_detail = ctk.CTkLabel(
            self.verdict, text="", font=font(11),
            text_color=TEXT_MUTED, anchor="w", wraplength=820, justify="left",
        )
        self.verdict_detail.pack(anchor="w", padx=20, pady=(0, 16))

    def _build_results_table(self) -> None:
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
            head, text="Ingredient requirements",
            font=font(14, "bold"), text_color=TEXT,
        ).pack(side="left")
        ctk.CTkLabel(
            head, text="What-if only  ·  nothing is deducted",
            font=font(11), text_color=TEXT_MUTED,
        ).pack(side="right")

        header = self._row_frame(panel, PANEL)
        header.pack(fill="x", padx=16, pady=(8, 6))
        for column, title in enumerate(RESULT_TITLES):
            self._cell(header, column, title, header=True)

        self.rows_frame = ctk.CTkFrame(panel, fg_color="transparent")
        self.rows_frame.pack(fill="x", padx=16, pady=(0, 8))

        self.results_note = ctk.CTkLabel(
            panel,
            text="",
            font=font(11),
            text_color=TEXT_MUTED,
            anchor="w",
            wraplength=820,
            justify="left",
        )
        self.results_note.pack(fill="x", padx=20, pady=(0, 16))

    def _build_actions(self) -> None:
        actions = ctk.CTkFrame(self.container, fg_color="transparent")
        actions.pack(fill="x", pady=(14, 0))
        ctk.CTkButton(
            actions,
            text="Record a stock-out on Inventory  →",
            command=self._go_inventory,
            width=250,
            height=38,
            corner_radius=9,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            text_color="#FFFFFF",
            font=font(12, "bold"),
        ).pack(side="left")
        for label, key in (("Edit recipe", "recipe"),
                           ("Manage ingredients", "ingredients")):
            ctk.CTkButton(
                actions,
                text=label,
                command=lambda destination=key: self._navigate(destination),
                width=150,
                height=38,
                corner_radius=9,
                fg_color=PANEL,
                hover_color="#F0E9DD",
                border_width=1,
                border_color=BORDER,
                text_color=TEXT_MUTED,
                font=font(12),
            ).pack(side="left", padx=8)

    def _build_footer(self) -> None:
        notes = (
            "This is a what-if calculation. NO stock is deducted and NO "
            "transaction is recorded when you press Calculate.",
            "To actually use ingredients, record a stock-out on the Inventory "
            "screen. Balances change only through stock-in / stock-out.",
            "Required = recipe quantity per serving × servings. "
            "'Can make now' is limited by the ingredient that runs out first.",
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
    # Calculation
    # ------------------------------------------------------------------
    def _use_preset(self, count: int) -> None:
        self.servings_entry.delete(0, "end")
        self.servings_entry.insert(0, str(count))
        self._calculate()

    def _calculate(self) -> None:
        try:
            result = usage.calculate(self.servings_entry.get())
        except (ValueError, sqlite3.Error) as exc:
            self.error_message.configure(text=str(exc), text_color=DANGER)
            return
        self.error_message.configure(text="", text_color=DANGER)
        self.last_result = result
        self._render(result)

    def _render(self, result: dict[str, Any]) -> None:
        count = result["servings"]
        self.context_label.configure(
            text=(
                f"{result['menu_item_name']}  ·  "
                f"{result['ingredient_count']} ingredient(s) in the recipe  ·  "
                "balances read live from SQLite"
            )
        )
        self._render_verdict(result)
        self._render_summary(result)
        self._render_rows(result["rows"])
        self.results_note.configure(
            text=(
                f"Required amounts for {count} serving(s). "
                "'Short by' shows the extra stock needed; "
                "'On hand' is the live balance, unchanged by this screen."
            )
        )

    def _render_verdict(self, result: dict[str, Any]) -> None:
        count = result["servings"]
        maximum = result["max_servings"]
        limiting = ", ".join(result["limiting_ingredients"]) or "—"
        if result["sufficient"]:
            self.verdict.configure(fg_color=VERDICT_OK_BG)
            self.verdict_title.configure(
                text=f"Enough stock for {count} serving(s) of Regular Ramen",
                text_color=SUCCESS,
            )
            self.verdict_detail.configure(
                text=(
                    f"Every recipe ingredient is available. Current stock can "
                    f"make up to {maximum} complete serving(s); "
                    f"limiting ingredient(s): {limiting}. "
                    "Nothing has been deducted."
                )
            )
        else:
            self.verdict.configure(fg_color=VERDICT_BAD_BG)
            self.verdict_title.configure(
                text=(
                    f"Cannot make {count} serving(s) — "
                    f"{result['shortage_count']} ingredient(s) short"
                ),
                text_color=DANGER,
            )
            missing = ", ".join(
                f"{row['name']} "
                f"({format_quantity(row['shortfall'], row['unit'])})"
                for row in result["shortages"][:4]
            )
            if len(result["shortages"]) > 4:
                missing += ", …"
            self.verdict_detail.configure(
                text=(
                    f"Short by: {missing}. Current stock can make up to "
                    f"{maximum} complete serving(s); limiting "
                    f"ingredient(s): {limiting}. Nothing has been deducted."
                )
            )

    def _render_summary(self, result: dict[str, Any]) -> None:
        for child in self.summary_frame.winfo_children():
            child.destroy()
        for column in range(4):
            self.summary_frame.grid_columnconfigure(column, weight=1)

        cards = (
            ("Requested servings", result["servings"], "Whole servings", ACCENT),
            ("Can make now", result["max_servings"],
             "Limited by the scarcest ingredient",
             SUCCESS if result["max_servings"] > 0 else DANGER),
            ("Ingredients short", result["shortage_count"],
             "Short or unavailable lines",
             DANGER if result["shortage_count"] else SUCCESS),
            ("Limiting ingredient",
             result["limiting_ingredients"][0]
             if result["limiting_ingredients"] else "—",
             "Runs out first", WARNING, 17),
        )
        for column, card in enumerate(cards):
            label, value, note, accent, *size = card
            self._stat_card(
                column, label, value, note, accent,
                value_size=size[0] if size else 26,
            )

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

    # ------------------------------------------------------------------
    # Table
    # ------------------------------------------------------------------
    @staticmethod
    def _row_frame(parent: Any, background: str) -> ctk.CTkFrame:
        row = ctk.CTkFrame(parent, fg_color=background, corner_radius=8)
        for index, weight in enumerate(RESULT_WEIGHTS):
            row.grid_columnconfigure(
                index,
                weight=weight,
                minsize=RESULT_MINSIZES[index],
                uniform="calculator_columns",
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

    def _render_rows(self, rows: list[dict[str, Any]]) -> None:
        for child in self.rows_frame.winfo_children():
            child.destroy()
        if not rows:
            empty = ctk.CTkFrame(self.rows_frame, fg_color=PANEL, corner_radius=10)
            empty.pack(fill="x", pady=(8, 12))
            ctk.CTkLabel(
                empty, text="The recipe has no ingredients yet",
                font=font(14, "bold"), text_color=TEXT,
            ).pack(pady=(18, 2))
            ctk.CTkLabel(
                empty,
                text="Add ingredients on the Recipe screen, then come back.",
                font=font(11), text_color=TEXT_MUTED,
            ).pack(pady=(0, 18))
            return

        for item in rows:
            row = self._row_frame(self.rows_frame, CARD)
            row.pack(fill="x", pady=2)
            self._cell(row, 0, item["name"])
            self._cell(row, 1, item["unit"])
            self._cell(row, 2, format_number(item["quantity_per_serving"]))
            self._cell(row, 3, format_number(item["quantity_required"]))
            self._cell(row, 4, format_number(item["quantity_on_hand"]))
            self._cell(
                row, 5,
                format_number(item["shortfall"])
                if item["shortfall"] > 0 else "—",
                color=DANGER if item["shortfall"] > 0 else TEXT_MUTED,
            )
            status = item["status"]
            colour = (
                SUCCESS if status == usage.STATUS_ENOUGH
                else DANGER if status == usage.STATUS_SHORT
                else WARNING
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
            ).grid(row=0, column=6, sticky="w", padx=6, pady=7)

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------
    def _navigate(self, key: str) -> None:
        if self.on_navigate is not None:
            self.on_navigate(key)

    def _go_inventory(self) -> None:
        self._navigate("inventory")
