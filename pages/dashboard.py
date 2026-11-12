"""
Stage 2 - Dashboard (Stage 8: aligned tables, alert strip, shared formatting).

Every value on this screen comes from SQLite. The four statistic cards and both
tables use database.get_dashboard_data(); the attention strip uses the Stage 7
read-only monitoring helper. No statistic is hardcoded.

The Refresh button in the header calls refresh(), which re-reads the database
and rebuilds the whole view.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable

import customtkinter as ctk

import database
import monitoring_repository
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
from formatting import format_quantity


class DashboardView(ctk.CTkFrame):
    def __init__(
        self,
        master: Any,
        user: dict[str, Any] | None = None,
        on_navigate: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(master, fg_color=CREAM, corner_radius=0)
        self.user = user
        self.on_navigate = on_navigate

        self.scroller = ctk.CTkScrollableFrame(
            self, fg_color=CREAM, corner_radius=0
        )
        self.scroller.pack(fill="both", expand=True)
        self.container = ctk.CTkFrame(self.scroller, fg_color="transparent")
        self.container.pack(fill="both", expand=True, padx=26, pady=22)
        self._render()

    def refresh(self) -> None:
        """Re-read all values from SQLite and redraw the dashboard."""
        self._render()

    def _clear(self) -> None:
        for child in self.container.winfo_children():
            child.destroy()

    # ------------------------------------------------------------------
    def _render(self) -> None:
        self._clear()
        try:
            data = database.get_dashboard_data()
        except sqlite3.Error as exc:
            ctk.CTkLabel(
                self.container,
                text=f"Could not load dashboard data: {exc}",
                font=font(13),
                text_color=DANGER,
                anchor="w",
            ).pack(anchor="w")
            return

        intro = ctk.CTkFrame(self.container, fg_color="transparent")
        intro.pack(fill="x", pady=(0, 14))
        ctk.CTkLabel(
            intro, text="Overview", font=font(20, "bold"), text_color=TEXT
        ).pack(side="left")
        ctk.CTkLabel(
            intro,
            text="Store:  Neighborhood Mart",
            font=font(12, "bold"),
            text_color=ACCENT,
        ).pack(side="right")

        # Stage 8: bring the Monitoring screen's alert counts onto the first
        # screen the user sees, with a one-click way to open the full list.
        self._alert_banner(self.container)

        cards = ctk.CTkFrame(self.container, fg_color="transparent")
        cards.pack(fill="x", pady=(0, 18))
        for column in range(4):
            cards.grid_columnconfigure(column, weight=1)

        self._stat_card(cards, 0, "Store", "Neighborhood Mart",
                        "Foods, drinks & everyday needs", ACCENT, value_size=17)
        self._stat_card(cards, 1, "Total products",
                        data["total_ingredients"],
                        "Active items in the catalog", ACCENT)
        self._stat_card(cards, 2, "Running low", data["low_stock"],
                        "At or below minimum stock", WARNING)
        self._stat_card(cards, 3, "Out of stock", data["out_of_stock"],
                        "Balance is zero", DANGER)

        self._activities_panel(self.container, data["recent_activities"])
        self._status_panel(self.container, data["ingredients"])

        ctk.CTkLabel(
            self.container,
            text="All figures are read live from the local SQLite database.",
            font=font(11),
            text_color=TEXT_MUTED,
            anchor="w",
        ).pack(anchor="w", pady=(2, 10))

    # ------------------------------------------------------------------
    def _alert_banner(self, parent: Any) -> None:
        """Optional read-only strip. A failure here must not hide the rest."""
        try:
            counts = monitoring_repository.get_alert_counts()
        except (sqlite3.Error, ValueError, KeyError, TypeError):
            return

        attention = int(counts["needs_attention"])
        strip = ctk.CTkFrame(
            parent,
            fg_color=PANEL if attention == 0 else "#FBEEE9",
            corner_radius=12,
            border_width=1,
            border_color=BORDER,
        )
        strip.pack(fill="x", pady=(0, 16))
        strip.grid_columnconfigure(0, weight=1)
        strip.grid_columnconfigure(1, weight=0)

        if attention == 0:
            text = (
                "Everything looks good  ·  no stock or expiry alerts "
                f"within {counts['warning_days']} days"
            )
            colour = SUCCESS
        else:
            text = (
                f"{attention} ingredient(s) need attention  ·  "
                f"{counts['critical']} critical  ·  "
                f"{counts['out_of_stock']} out of stock  ·  "
                f"{counts['low_stock']} low stock  ·  "
                f"{counts['expired']} expired  ·  "
                f"{counts['near_expiry']} near expiry"
            )
            colour = DANGER if counts["critical"] else WARNING

        ctk.CTkLabel(
            strip,
            text=text,
            font=font(12, "bold"),
            text_color=colour,
            anchor="w",
            wraplength=740,
            justify="left",
        ).grid(row=0, column=0, sticky="w", padx=18, pady=14)

        if self.on_navigate is not None:
            ctk.CTkButton(
                strip,
                text="Open monitoring",
                command=lambda: self.on_navigate("monitoring"),
                width=150,
                height=32,
                corner_radius=8,
                fg_color=ACCENT,
                hover_color=ACCENT_HOVER,
                text_color="#FFFFFF",
                font=font(12, "bold"),
            ).grid(row=0, column=1, sticky="e", padx=(8, 18), pady=14)

    # ------------------------------------------------------------------
    def _stat_card(
        self,
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
            wraplength=170,
            justify="left",
        ).pack(fill="x", padx=18, pady=(2, 0))
        ctk.CTkLabel(
            card,
            text=note,
            font=font(11),
            text_color=TEXT_MUTED,
            anchor="w",
            wraplength=170,
            justify="left",
        ).pack(fill="x", padx=18, pady=(2, 16))

    def _panel(self, parent: Any, title: str, subtitle: str) -> ctk.CTkFrame:
        panel = ctk.CTkFrame(
            parent,
            fg_color=CARD,
            corner_radius=14,
            border_width=1,
            border_color=BORDER,
        )
        panel.pack(fill="x", pady=(0, 16))
        head = ctk.CTkFrame(panel, fg_color="transparent")
        head.pack(fill="x", padx=20, pady=(16, 10))
        ctk.CTkLabel(
            head, text=title, font=font(15, "bold"), text_color=TEXT
        ).pack(side="left")
        ctk.CTkLabel(
            head, text=subtitle, font=font(11), text_color=TEXT_MUTED
        ).pack(side="right")
        return panel

    @staticmethod
    def _grid_row(
        parent: Any, weights: tuple[int, ...], background: str = CARD
    ) -> ctk.CTkFrame:
        """Every row is its OWN grid, so all rows need identical minsize +
        uniform groups or the header and data columns drift apart (Stage 8)."""
        row = ctk.CTkFrame(parent, fg_color=background, corner_radius=8)
        row.pack(fill="x", padx=20, pady=2)
        group = f"dashboard_{len(weights)}"
        for index, weight in enumerate(weights):
            row.grid_columnconfigure(
                index,
                weight=weight,
                minsize=70 * weight,
                uniform=group,
            )
        return row

    @staticmethod
    def _cell(row: Any, column: int, text: str, *, header: bool = False) -> None:
        ctk.CTkLabel(
            row,
            text=text,
            font=font(11, "bold") if header else font(12),
            text_color=TEXT_MUTED if header else TEXT,
            anchor="w",
        ).grid(row=0, column=column, sticky="w", padx=8, pady=6)

    # ------------------------------------------------------------------
    def _activities_panel(self, parent: Any, activities: list[dict[str, Any]]) -> None:
        panel = self._panel(
            parent,
            "Recent inventory activities",
            "Last 5 stock movements",
        )
        weights = (2, 1, 2, 2, 2)
        header = self._grid_row(panel, weights, PANEL)
        for column, text in enumerate(
            ("Date", "Type", "Ingredient", "Quantity", "Reason")
        ):
            self._cell(header, column, text, header=True)

        if not activities:
            empty = ctk.CTkFrame(panel, fg_color=PANEL, corner_radius=10)
            empty.pack(fill="x", padx=20, pady=(8, 20))
            ctk.CTkLabel(
                empty,
                text="No inventory activity yet",
                font=font(14, "bold"),
                text_color=TEXT,
            ).pack(pady=(20, 2))
            ctk.CTkLabel(
                empty,
                text=(
                    "Record a stock-in or stock-out on the Inventory "
                    "screen to see activity here."
                ),
                font=font(11),
                text_color=TEXT_MUTED,
            ).pack(pady=(0, 20))
        else:
            for activity in activities:
                row = self._grid_row(panel, weights)
                movement = activity["movement_type"]
                self._cell(row, 0, str(activity["transaction_date"]))
                ctk.CTkLabel(
                    row,
                    text=movement,
                    fg_color=SUCCESS if movement == "IN" else ACCENT,
                    text_color="#FFFFFF",
                    corner_radius=6,
                    width=44,
                    height=22,
                    font=font(11, "bold"),
                ).grid(row=0, column=1, sticky="w", padx=8, pady=6)
                self._cell(row, 2, str(activity["ingredient_name"]))
                self._cell(
                    row, 3,
                    format_quantity(
                        activity["quantity"], activity["unit"], signed=True
                    ),
                )
                self._cell(row, 4, str(activity["reason"] or "—"))
            ctk.CTkFrame(panel, fg_color="transparent", height=12).pack(fill="x")

    def _status_panel(self, parent: Any, ingredients: list[dict[str, Any]]) -> None:
        panel = self._panel(
            parent,
            "Ingredient status",
            f"{len(ingredients)} active ingredients",
        )
        weights = (3, 2, 2, 2)
        header = self._grid_row(panel, weights, PANEL)
        for column, text in enumerate(
            ("Ingredient", "On hand", "Minimum", "Status")
        ):
            self._cell(header, column, text, header=True)

        for item in ingredients:
            row = self._grid_row(panel, weights)
            self._cell(row, 0, str(item["name"]))
            self._cell(
                row, 1,
                format_quantity(item["quantity_on_hand"], item["unit"]),
            )
            self._cell(
                row, 2,
                format_quantity(item["minimum_stock"], item["unit"]),
            )
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
                width=96,
                height=22,
                font=font(11, "bold"),
            ).grid(row=0, column=3, sticky="w", padx=8, pady=6)

        ctk.CTkFrame(panel, fg_color="transparent", height=6).pack(fill="x")
        ctk.CTkLabel(
            panel,
            text=(
                "Status rule: 0 = out of stock  ·  "
                "0 < on hand ≤ minimum = low stock"
            ),
            font=font(11),
            text_color=TEXT_MUTED,
            anchor="w",
        ).pack(anchor="w", padx=20, pady=(2, 16))
