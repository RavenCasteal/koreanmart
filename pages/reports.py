"""
Stage 13 - Sales reports / daily closing (Z-read) screen. MANAGER ONLY.

The cashier never gets a sidebar button for this page and cannot open it
through AppShell.navigate(). As a second line of defence every read below
passes `actor=self.user` into sales_report, whose functions call
permissions.require(CAP_VIEW_REPORTS), so sales totals cannot leak to a
cashier session even if a widget is constructed directly.

Layout (all inside one vertically scrollable page):
    date navigation + Print / save Z-read + Export CSV
    five KPI cards
    payment mix / cashier totals / top products / category mix tables
    a 7-day sales trend with proportional bars
    the day's transaction list (completed and voided, newest first)

All money is Philippine peso (PHP) and goes through formatting.format_money().
Nothing here writes to a sale: the only files produced are reports/*.txt
(Z-read) and reports/*.csv (transaction export).
"""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any, Callable

import customtkinter as ctk

import permissions
import sales_report
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
from formatting import CURRENCY_CODE, CURRENCY_SYMBOL, format_money

# grid "uniform" scales columns by max(minsize / weight) * sum(weights).
# Keeping max(minsize / weight) near 10 keeps every table inside the
# 1160x720 window (the same fix applied to the Stage 12 POS tables).
SMALL_WEIGHTS = (16, 8, 10)
SMALL_MINSIZES = (150, 70, 90)
TREND_WEIGHTS = (10, 7, 11, 14)
TREND_MINSIZES = (100, 60, 100, 120)
LIST_WEIGHTS = (16, 12, 10, 6, 10, 9, 8)
LIST_MINSIZES = (150, 110, 90, 55, 90, 80, 75)
LIST_TITLES = (
    "Transaction", "Date / time", "Cashier", "Items", "Total",
    "Payment", "Status",
)


class ReportsView(ctk.CTkFrame):
    """Read-only daily sales report for the manager."""

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
        self.allowed = permissions.can(
            self.user, permissions.CAP_VIEW_REPORTS
        )
        self.day = date.today().isoformat()
        self.summary: dict[str, Any] | None = None
        # GUI smoke runs turn printing off; the Z-read is still archived.
        self.auto_print = True
        self.report_dir: Any = None

        self.scroller = ctk.CTkScrollableFrame(
            self, fg_color=CREAM, corner_radius=0
        )
        self.scroller.pack(fill="both", expand=True)
        self.container = ctk.CTkFrame(self.scroller, fg_color="transparent")
        self.container.pack(fill="both", expand=True, padx=26, pady=20)

        self._build_header()
        self.body = ctk.CTkFrame(self.container, fg_color="transparent")
        self.body.pack(fill="x")
        if not self.allowed:
            self._build_denied()
            return
        self._build_controls()
        self.refresh()

    # ------------------------------------------------------------------
    # Fixed header / controls
    # ------------------------------------------------------------------
    def _build_header(self) -> None:
        head = ctk.CTkFrame(self.container, fg_color="transparent")
        head.pack(fill="x", pady=(0, 10))
        ctk.CTkLabel(
            head, text="Sales reports", font=font(20, "bold"), text_color=TEXT
        ).pack(side="left")
        ctk.CTkLabel(
            head,
            text=f"Daily closing / Z-read  ·  {CURRENCY_CODE} / "
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

    def _build_controls(self) -> None:
        card = ctk.CTkFrame(
            self.container, fg_color=CARD, corner_radius=14,
            border_width=1, border_color=BORDER,
        )
        card.pack(fill="x", pady=(0, 16))

        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=18, pady=16)
        ctk.CTkLabel(
            row, text="Business date", font=font(11, "bold"),
            text_color=TEXT_MUTED,
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            row, text="‹", command=lambda: self._change_day(-1),
            width=32, height=34, corner_radius=8,
            fg_color=PANEL, hover_color="#F0E9DD",
            border_width=1, border_color=BORDER,
            text_color=TEXT, font=font(14, "bold"),
        ).pack(side="left", padx=(0, 4))
        self.date_entry = ctk.CTkEntry(
            row, width=130, height=34, corner_radius=9,
            fg_color=PANEL, border_width=1, border_color=ACCENT,
            text_color=TEXT, font=font(13), justify="center",
        )
        self.date_entry.insert(0, self.day)
        self.date_entry.bind("<Return>", lambda _event: self.refresh())
        self.date_entry.pack(side="left", padx=(0, 4))
        ctk.CTkButton(
            row, text="›", command=lambda: self._change_day(1),
            width=32, height=34, corner_radius=8,
            fg_color=PANEL, hover_color="#F0E9DD",
            border_width=1, border_color=BORDER,
            text_color=TEXT, font=font(14, "bold"),
        ).pack(side="left", padx=(0, 8))
        for label, command in (
            ("Today", self._today),
            ("Refresh", self.refresh),
        ):
            ctk.CTkButton(
                row, text=label, command=command,
                width=80, height=34, corner_radius=8,
                fg_color=PANEL, hover_color="#F0E9DD",
                border_width=1, border_color=BORDER,
                text_color=TEXT, font=font(11),
            ).pack(side="left", padx=(0, 6))

        ctk.CTkButton(
            row, text="Export CSV", command=self._export_csv,
            width=110, height=34, corner_radius=8,
            fg_color=PANEL, hover_color="#F0E9DD",
            border_width=1, border_color=BORDER,
            text_color=TEXT, font=font(11, "bold"),
        ).pack(side="right")
        ctk.CTkButton(
            row, text="Print / save Z-read", command=self._print_z,
            width=160, height=34, corner_radius=8,
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
            text_color="#FFFFFF", font=font(11, "bold"),
        ).pack(side="right", padx=(0, 8))

    def _build_denied(self) -> None:
        card = ctk.CTkFrame(
            self.body, fg_color=CARD, corner_radius=14,
            border_width=1, border_color=BORDER,
        )
        card.pack(fill="x", pady=8)
        ctk.CTkLabel(
            card, text="Manager access required",
            font=font(16, "bold"), text_color=DANGER,
        ).pack(anchor="w", padx=20, pady=(20, 4))
        ctk.CTkLabel(
            card,
            text="Sales totals and end-of-day reports are only available "
                 "to a manager. Ask the manager to sign in on this PC.",
            font=font(12), text_color=TEXT_MUTED,
            anchor="w", wraplength=700, justify="left",
        ).pack(anchor="w", padx=20, pady=(0, 20))

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------
    def _message(self, text: str, *, error: bool = False) -> None:
        self.status_label.configure(
            text=text, text_color=DANGER if error else SUCCESS
        )

    def refresh(self) -> None:
        """Re-read the selected day from SQLite and rebuild the report."""
        if not self.allowed:
            self._message("Only a manager can view sales reports.", error=True)
            return
        try:
            day = sales_report.normalize_date(self.date_entry.get())
            summary = sales_report.daily_summary(day, actor=self.actor)
            trend = sales_report.sales_trend(
                7, end_date=day, actor=self.actor
            )
        except (
            ValueError, sqlite3.Error, permissions.PermissionDenied
        ) as exc:
            self._message(str(exc), error=True)
            return
        self.day = summary["date"]
        self.summary = summary
        self.date_entry.delete(0, "end")
        self.date_entry.insert(0, self.day)
        self._render_body(summary, trend)
        self._message(
            f"{self.day}: {summary['transaction_count']} sale(s), "
            f"net sales {format_money(summary['net_sales'])}."
        )

    def _change_day(self, delta: int) -> None:
        try:
            self.day = sales_report.shift_date(self.day, delta)
        except ValueError as exc:
            self._message(str(exc), error=True)
            return
        self.date_entry.delete(0, "end")
        self.date_entry.insert(0, self.day)
        self.refresh()

    def _today(self) -> None:
        self.day = date.today().isoformat()
        self.date_entry.delete(0, "end")
        self.date_entry.insert(0, self.day)
        self.refresh()

    def _print_z(self) -> None:
        if self.summary is None:
            self._message("Load a report first.", error=True)
            return
        try:
            outcome = sales_report.issue_z_read(
                self.summary,
                auto_print=self.auto_print,
                base_dir=self.report_dir,
                actor=self.actor,
            )
        except (OSError, ValueError, permissions.PermissionDenied) as exc:
            self._message(str(exc), error=True)
            return
        self._message(
            f"Z-read saved to {outcome['path'].name}. "
            + outcome["print"]["detail"]
        )

    def _export_csv(self) -> None:
        try:
            path = sales_report.export_transactions_csv(
                self.day, base_dir=self.report_dir, actor=self.actor
            )
        except (
            OSError, ValueError, sqlite3.Error, permissions.PermissionDenied
        ) as exc:
            self._message(str(exc), error=True)
            return
        self._message(f"Transactions exported to {path.name}.")

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    def _render_body(
        self, summary: dict[str, Any], trend: list[dict[str, Any]]
    ) -> None:
        for child in self.body.winfo_children():
            child.destroy()

        cards = ctk.CTkFrame(self.body, fg_color="transparent")
        cards.pack(fill="x", pady=(0, 16))
        for column in range(6):
            cards.grid_columnconfigure(column, weight=1)
        self._kpi(cards, 0, "Gross sales",
                  format_money(summary["gross_sales"]),
                  "Before discounts", ACCENT)
        self._kpi(cards, 1, "Voids",
                  format_money(summary["voided_amount"]),
                  f"{summary['voided_count']} voided sale(s)", DANGER)
        self._kpi(cards, 2, "Discounts",
                  format_money(summary["discount_amount"]),
                  f"{summary['discount_count']} SC / PWD sale(s)", WARNING,
                  value_size=22)
        self._kpi(cards, 3, "Net sales",
                  format_money(summary["net_sales"]),
                  "After discounts", SUCCESS, value_size=22)
        self._kpi(cards, 4, "Transactions",
                  str(summary["transaction_count"]),
                  f"{summary['completed_count']} completed · "
                  f"{summary['items_sold']} items sold", ACCENT)
        self._kpi(cards, 5, "Average basket",
                  format_money(summary["average_basket"]),
                  "Per completed sale", ACCENT, value_size=22)

        top = self._pair_row()
        self._table_panel(
            top[0], "Payment mix", "Completed sales by method",
            ("Method", "Sales", "Total"),
            "payment",
            [
                (row["name"], str(row["transactions"]),
                 format_money(row["total"]))
                for row in summary["by_payment"]
            ],
        )
        self._table_panel(
            top[1], "Cashier totals", "Completed sales by account",
            ("Cashier", "Sales", "Total"),
            "cashier",
            [
                (row["name"], str(row["transactions"]),
                 format_money(row["total"]))
                for row in summary["by_cashier"]
            ],
        )

        second = self._pair_row()
        self._table_panel(
            second[0], "Top products", "By units sold",
            ("Product", "Units", "Sales total"),
            "products",
            [
                (row["name"], str(row["quantity"]),
                 format_money(row["total"]))
                for row in summary["top_products"]
            ],
        )
        self._table_panel(
            second[1], "Category mix", "Completed sale lines",
            ("Category", "Units", "Sales total"),
            "category",
            [
                (row["name"], str(row["quantity"]),
                 format_money(row["total"]))
                for row in summary["by_category"]
            ],
        )

        self._trend_panel(trend)
        self._transactions_panel(summary["transactions"])

        ctk.CTkLabel(
            self.body,
            text="Figures come from immutable sale snapshots in the local "
                 "SQLite database. Gross sales are before the 20% Senior / PWD "
                 "discount; voided sales are kept for audit and never deleted.",
            font=font(11), text_color=TEXT_MUTED,
            anchor="w", wraplength=820, justify="left",
        ).pack(anchor="w", pady=(2, 10))

    def _kpi(
        self,
        parent: Any,
        column: int,
        label: str,
        value: str,
        note: str,
        accent: str,
        value_size: int = 20,
    ) -> None:
        card = ctk.CTkFrame(
            parent, fg_color=CARD, corner_radius=14,
            border_width=1, border_color=BORDER,
        )
        card.grid(row=0, column=column, sticky="nsew", padx=(0, 10))
        ctk.CTkFrame(
            card, fg_color=accent, height=3, corner_radius=2
        ).pack(fill="x", padx=14, pady=(14, 0))
        ctk.CTkLabel(
            card, text=label, font=font(11), text_color=TEXT_MUTED,
            anchor="w",
        ).pack(fill="x", padx=14, pady=(10, 0))
        value_label = ctk.CTkLabel(
            card, text=value, font=font(value_size, "bold"),
            text_color=TEXT, anchor="w", wraplength=90, justify="left",
        )
        value_label.pack(fill="x", padx=14, pady=(2, 0))
        note_label = ctk.CTkLabel(
            card, text=note, font=font(10), text_color=TEXT_MUTED,
            anchor="w", wraplength=90, justify="left",
        )
        note_label.pack(fill="x", padx=14, pady=(2, 14))

        # Stage 15 added a sixth KPI, so each card is narrower than the old
        # fixed 150px wraplength. Fit the wrap to the REAL card width: long
        # notes (e.g. "2 completed - 4 items sold") now wrap instead of being
        # clipped on a 1160px counter PC, while wide windows use the space.
        fitted = {"width": 90}

        def _fit_labels(_event: Any = None) -> None:
            width = max(48, int(card.winfo_width()) - 28)
            if width == fitted["width"]:
                return
            fitted["width"] = width
            value_label.configure(wraplength=width)
            note_label.configure(wraplength=width)

        card.bind("<Configure>", _fit_labels, add="+")

    def _pair_row(self) -> tuple[Any, Any]:
        row = ctk.CTkFrame(self.body, fg_color="transparent")
        row.pack(fill="x")
        left = ctk.CTkFrame(row, fg_color="transparent")
        left.pack(side="left", fill="both", expand=True, padx=(0, 8))
        right = ctk.CTkFrame(row, fg_color="transparent")
        right.pack(side="left", fill="both", expand=True)
        return left, right

    def _panel(self, parent: Any, title: str, subtitle: str) -> Any:
        card = ctk.CTkFrame(
            parent, fg_color=CARD, corner_radius=14,
            border_width=1, border_color=BORDER,
        )
        card.pack(fill="x", pady=(0, 16))
        head = ctk.CTkFrame(card, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(14, 8))
        ctk.CTkLabel(
            head, text=title, font=font(14, "bold"), text_color=TEXT
        ).pack(side="left")
        ctk.CTkLabel(
            head, text=subtitle, font=font(10), text_color=TEXT_MUTED
        ).pack(side="right")
        return card

    def _table_panel(
        self,
        parent: Any,
        title: str,
        subtitle: str,
        titles: tuple[str, ...],
        group: str,
        rows: list[tuple[str, str, str]],
    ) -> None:
        card = self._panel(parent, title, subtitle)
        header = self._grid_row(
            card, PANEL, SMALL_WEIGHTS, SMALL_MINSIZES, f"{group}_head"
        )
        header.pack(fill="x", padx=16)
        for column, text in enumerate(titles):
            self._cell(header, column, text, header=True)
        body = ctk.CTkFrame(card, fg_color="transparent")
        body.pack(fill="x", padx=16, pady=(0, 14))
        if not rows:
            ctk.CTkLabel(
                body, text="No completed sales for this date.",
                font=font(11), text_color=TEXT_MUTED, anchor="w",
            ).pack(anchor="w", pady=8)
            return
        for values in rows:
            line = self._grid_row(
                body, CARD, SMALL_WEIGHTS, SMALL_MINSIZES, group
            )
            line.pack(fill="x", pady=2)
            for column, value in enumerate(values):
                self._cell(line, column, value)

    def _trend_panel(self, trend: list[dict[str, Any]]) -> None:
        card = self._panel(
            self.body, "Last 7 days", "Net sales (completed only)"
        )
        head = self._grid_row(
            card, PANEL, TREND_WEIGHTS, TREND_MINSIZES, "trend_head"
        )
        head.pack(fill="x", padx=16)
        for column, text in enumerate(
            ("Date", "Sales", "Net sales", "Relative")
        ):
            self._cell(head, column, text, header=True)
        body = ctk.CTkFrame(card, fg_color="transparent")
        body.pack(fill="x", padx=16, pady=(0, 10))
        peak = max((float(row["net_sales"]) for row in trend), default=0.0)
        for entry in trend:
            line = self._grid_row(
                body, CARD, TREND_WEIGHTS, TREND_MINSIZES, "trend"
            )
            line.pack(fill="x", pady=2)
            self._cell(line, 0, str(entry["date"]))
            self._cell(line, 1, str(entry["transactions"]))
            self._cell(line, 2, format_money(entry["net_sales"]))
            bar = ctk.CTkProgressBar(
                line, height=10, corner_radius=5,
                fg_color=PANEL, progress_color=ACCENT,
            )
            bar.set(
                float(entry["net_sales"]) / peak if peak > 0 else 0.0
            )
            bar.grid(row=0, column=3, sticky="ew", padx=8, pady=10)
        ctk.CTkLabel(
            card,
            text="Bars are scaled to the busiest day in this 7-day range.",
            font=font(10), text_color=TEXT_MUTED, anchor="w",
        ).pack(anchor="w", padx=16, pady=(0, 12))

    def _transactions_panel(self, rows: list[dict[str, Any]]) -> None:
        card = self._panel(
            self.body, "Transactions", "Newest first · voided sales kept"
        )
        header = self._grid_row(
            card, PANEL, LIST_WEIGHTS, LIST_MINSIZES, "list_head"
        )
        header.pack(fill="x", padx=16)
        for column, text in enumerate(LIST_TITLES):
            self._cell(header, column, text, header=True)
        body = ctk.CTkFrame(card, fg_color="transparent")
        body.pack(fill="x", padx=16, pady=(0, 14))
        if not rows:
            ctk.CTkLabel(
                body, text="No sales were recorded on this date.",
                font=font(11), text_color=TEXT_MUTED, anchor="w",
            ).pack(anchor="w", pady=8)
            return
        for item in rows:
            line = self._grid_row(
                body, CARD, LIST_WEIGHTS, LIST_MINSIZES, "list"
            )
            line.pack(fill="x", pady=2)
            values = (
                str(item["transaction_number"]),
                str(item["transaction_date"]),
                str(item["cashier"]),
                str(item["item_count"]),
                format_money(item["total_amount"]),
                str(item["payment_method"]),
                "Voided" if item["status"] == "voided" else "Completed",
            )
            for column, value in enumerate(values):
                colour = None
                if column == 6:
                    colour = (
                        DANGER if item["status"] == "voided" else SUCCESS
                    )
                self._cell(line, column, value, color=colour)

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
    ) -> Any:
        row = ctk.CTkFrame(parent, fg_color=background, corner_radius=8)
        for index, weight in enumerate(weights):
            row.grid_columnconfigure(
                index, weight=weight, minsize=minsizes[index], uniform=group
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
            wraplength=175,
            justify="left",
        ).grid(row=0, column=column, sticky="w", padx=6, pady=7)
