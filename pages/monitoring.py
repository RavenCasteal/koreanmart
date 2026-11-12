"""
Stage 7 - low-stock and expiration monitoring.

A single read-only screen showing which ACTIVE ingredients need attention:
out of stock, at or below minimum, expiring within 7 days, or already
expired. The most urgent items are sorted first.

Nothing here changes the database. To fix an alert the user goes to the
Ingredients screen (minimum / expiry date) or the Inventory screen
(record a stock-in). All SQL lives in monitoring_repository.py.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable

import customtkinter as ctk

import monitoring_repository as monitoring
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
from formatting import format_number

MONITOR_WEIGHTS = (3, 1, 2, 2, 2, 3, 2)
MONITOR_MINSIZES = (148, 56, 92, 88, 108, 172, 100)
MONITOR_TITLES = (
    "Ingredient", "Unit", "On hand", "Minimum", "Stock", "Expiry", "Priority",
)

FILTER_LABELS = tuple(monitoring.FILTER_CODES)

BANNER_OK_BG = "#EAF4EE"
BANNER_ALERT_BG = "#FBEAE6"


class MonitoringView(ctk.CTkFrame):
    """Summary cards + one searchable, filterable alert table."""

    def __init__(
        self,
        master: Any,
        user: dict[str, Any] | None = None,
        on_navigate: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(master, fg_color=CREAM, corner_radius=0)
        self.user = user
        self.on_navigate = on_navigate

        self.scroller = ctk.CTkScrollableFrame(self, fg_color=CREAM, corner_radius=0)
        self.scroller.pack(fill="both", expand=True)
        self.container = ctk.CTkFrame(self.scroller, fg_color="transparent")
        self.container.pack(fill="both", expand=True, padx=26, pady=22)

        # Static layout built once so live search keeps keyboard focus.
        self._build_toolbar()
        self.summary_frame = ctk.CTkFrame(self.container, fg_color="transparent")
        self.summary_frame.pack(fill="x", pady=(0, 16))
        self._build_banner()
        self._build_table_shell()
        self._build_actions()
        self._build_footer()
        self._load()

    def refresh(self) -> None:
        """Header Refresh re-reads every balance and expiry date from SQLite."""
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
            left, text="Stock & expiry monitoring",
            font=font(20, "bold"), text_color=TEXT,
        ).pack(anchor="w")
        self.count_label = ctk.CTkLabel(
            left, text="", font=font(11), text_color=TEXT_MUTED, anchor="w"
        )
        self.count_label.pack(anchor="w")

        right = ctk.CTkFrame(top, fg_color="transparent")
        right.pack(side="right")
        ctk.CTkButton(
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
        ).pack(side="right")
        self.filter_combo = ctk.CTkComboBox(
            right,
            values=list(FILTER_LABELS),
            width=170,
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
            placeholder_text="Search ingredient, status, date…",
            width=240,
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

    def _build_banner(self) -> None:
        self.banner = ctk.CTkFrame(
            self.container,
            fg_color=BANNER_ALERT_BG,
            corner_radius=14,
            border_width=1,
            border_color=BORDER,
        )
        self.banner.pack(fill="x", pady=(0, 16))
        self.banner_title = ctk.CTkLabel(
            self.banner, text="", font=font(16, "bold"),
            text_color=DANGER, anchor="w", wraplength=820, justify="left",
        )
        self.banner_title.pack(anchor="w", padx=20, pady=(16, 2))
        self.banner_detail = ctk.CTkLabel(
            self.banner, text="", font=font(11),
            text_color=TEXT_MUTED, anchor="w", wraplength=820, justify="left",
        )
        self.banner_detail.pack(anchor="w", padx=20, pady=(0, 16))

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
            head, text="Ingredient watchlist",
            font=font(14, "bold"), text_color=TEXT,
        ).pack(side="left")
        ctk.CTkLabel(
            head, text="Most urgent first  ·  read-only",
            font=font(11), text_color=TEXT_MUTED,
        ).pack(side="right")

        header = self._row_frame(self.panel, PANEL)
        header.pack(fill="x", padx=16, pady=(8, 6))
        for column, title in enumerate(MONITOR_TITLES):
            self._cell(header, column, title, header=True)

        self.rows_frame = ctk.CTkFrame(self.panel, fg_color="transparent")
        self.rows_frame.pack(fill="x", padx=16, pady=(0, 8))
        self.message_label = ctk.CTkLabel(
            self.panel,
            text="",
            font=font(11),
            text_color=TEXT_MUTED,
            anchor="w",
            wraplength=820,
            justify="left",
        )
        self.message_label.pack(fill="x", padx=20, pady=(0, 16))

    def _build_actions(self) -> None:
        actions = ctk.CTkFrame(self.container, fg_color="transparent")
        actions.pack(fill="x", pady=(14, 0))
        for index, (label, key) in enumerate((
            ("Record a stock-in  →", "inventory"),
            ("Manage ingredients", "ingredients"),
            ("Open calculator", "calculator"),
        )):
            primary = index == 0
            ctk.CTkButton(
                actions,
                text=label,
                command=lambda destination=key: self._navigate(destination),
                width=180,
                height=38,
                corner_radius=9,
                fg_color=ACCENT if primary else PANEL,
                hover_color=ACCENT_HOVER if primary else "#F0E9DD",
                border_width=0 if primary else 1,
                border_color=BORDER,
                text_color="#FFFFFF" if primary else TEXT_MUTED,
                font=font(12, "bold" if primary else "normal"),
            ).pack(side="left", padx=(0, 8))

    def _build_footer(self) -> None:
        notes = (
            "Near expiry means within "
            f"{monitoring.EXPIRY_WARNING_DAYS} days INCLUDING today; "
            "expired means any date before today.",
            "Monitoring is read-only. Fix an alert on the Ingredients screen "
            "(minimum / expiry) or the Inventory screen (record a stock-in).",
            "Soft-deleted ingredients are excluded because they are historical "
            "records. Expiry is tracked per ingredient, not per batch.",
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
    # Data load
    # ------------------------------------------------------------------
    def _clear_filters(self) -> None:
        self.search_entry.delete(0, "end")
        self.filter_combo.set(FILTER_LABELS[0])
        self._load()
        self.search_entry.focus_set()

    def _load(self) -> None:
        label = self.filter_combo.get()
        try:
            data = monitoring.get_monitoring_data(
                self.search_entry.get(), label
            )
        except (ValueError, sqlite3.Error) as exc:
            self.message_label.configure(
                text=f"Could not load monitoring data: {exc}",
                text_color=DANGER,
            )
            return

        summary = data["summary"]
        self.count_label.configure(
            text=(
                f"{data['shown']} shown  ·  {data['total']} active "
                f"ingredient(s)  ·  as of {data['today']}"
            )
        )
        self.message_label.configure(
            text=(
                f"Showing {data['shown']} of {data['total']} active "
                f"ingredient(s). {summary['needs_attention']} need attention."
            ),
            text_color=TEXT_MUTED,
        )
        self._render_summary(summary)
        self._render_banner(summary)
        self._render_rows(data["rows"])

    def _render_summary(self, summary: dict[str, Any]) -> None:
        for child in self.summary_frame.winfo_children():
            child.destroy()
        for column in range(4):
            self.summary_frame.grid_columnconfigure(column, weight=1)
        cards = (
            ("Out of stock", summary["out_of_stock"],
             "Balance is zero", DANGER),
            ("Low stock", summary["low_stock"],
             "At or below minimum", WARNING),
            ("Near expiry", summary["near_expiry"],
             f"Within {summary['warning_days']} days", WARNING),
            ("Expired", summary["expired"],
             "Past the expiry date", DANGER),
        )
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
            card, text=str(value), font=font(26, "bold"),
            text_color=TEXT, anchor="w",
        ).pack(fill="x", padx=18, pady=(2, 0))
        ctk.CTkLabel(
            card, text=note, font=font(11), text_color=TEXT_MUTED,
            anchor="w", wraplength=200, justify="left",
        ).pack(fill="x", padx=18, pady=(2, 16))

    def _render_banner(self, summary: dict[str, Any]) -> None:
        attention = summary["needs_attention"]
        if attention:
            self.banner.configure(fg_color=BANNER_ALERT_BG)
            self.banner_title.configure(
                text=f"{attention} ingredient(s) need attention",
                text_color=DANGER,
            )
            self.banner_detail.configure(
                text=(
                    f"{summary['critical']} critical  ·  "
                    f"{summary['stock_alerts']} stock alert(s)  ·  "
                    f"{summary['expiry_alerts']} expiry alert(s). "
                    "Critical items are out of stock or already expired."
                )
            )
        else:
            self.banner.configure(fg_color=BANNER_OK_BG)
            self.banner_title.configure(
                text="Everything looks good - no active alerts",
                text_color=SUCCESS,
            )
            self.banner_detail.configure(
                text=(
                    "All active ingredients are above their minimum stock and "
                    "have no expiry date within the warning window."
                )
            )

    # ------------------------------------------------------------------
    # Table
    # ------------------------------------------------------------------
    @staticmethod
    def _row_frame(parent: Any, background: str) -> ctk.CTkFrame:
        row = ctk.CTkFrame(parent, fg_color=background, corner_radius=8)
        for index, weight in enumerate(MONITOR_WEIGHTS):
            row.grid_columnconfigure(
                index,
                weight=weight,
                minsize=MONITOR_MINSIZES[index],
                uniform="monitor_columns",
            )
        return row

    @staticmethod
    def _cell(
        row: Any, column: int, text: str, *, header: bool = False,
        color: str | None = None,
    ) -> None:
        ctk.CTkLabel(
            row, text=text,
            font=font(11, "bold") if header else font(12),
            text_color=color or (TEXT_MUTED if header else TEXT),
            anchor="w", wraplength=160, justify="left",
        ).grid(row=0, column=column, sticky="w", padx=6, pady=7)

    def _render_rows(self, rows: list[dict[str, Any]]) -> None:
        for child in self.rows_frame.winfo_children():
            child.destroy()
        if not rows:
            empty = ctk.CTkFrame(self.rows_frame, fg_color=PANEL, corner_radius=10)
            empty.pack(fill="x", pady=(8, 12))
            ctk.CTkLabel(
                empty, text="No ingredients match",
                font=font(14, "bold"), text_color=TEXT,
            ).pack(pady=(18, 2))
            ctk.CTkLabel(
                empty,
                text=(
                    "Clear the search, choose another filter, or add "
                    "ingredients on the Ingredients screen."
                ),
                font=font(11), text_color=TEXT_MUTED, wraplength=600,
            ).pack(pady=(0, 18))
            return

        for item in rows:
            row = self._row_frame(self.rows_frame, CARD)
            row.pack(fill="x", pady=2)
            self._cell(row, 0, item["name"])
            self._cell(row, 1, item["unit"])
            self._cell(row, 2, format_number(item["quantity_on_hand"]))
            self._cell(row, 3, format_number(item["minimum_stock"]))
            self._cell(
                row, 4, item["status"],
                color=(DANGER if item["status"] == "Out of stock"
                       else WARNING if item["status"] == "Low stock"
                       else SUCCESS),
            )
            expiry_colour = (
                DANGER if item["expiry_status"] in ("Expired", "Invalid")
                else WARNING if item["expiry_status"] == "Near expiry"
                else TEXT_MUTED
            )
            self._cell(row, 5, item["expiry_text"], color=expiry_colour)
            severity = item["severity"]
            badge = (
                DANGER if severity == monitoring.SEVERITY_CRITICAL
                else WARNING if severity == monitoring.SEVERITY_WARNING
                else SUCCESS
            )
            ctk.CTkLabel(
                row, text=severity, fg_color=badge, text_color="#FFFFFF",
                corner_radius=6, width=82, height=22,
                font=font(11, "bold"),
            ).grid(row=0, column=6, sticky="w", padx=6, pady=7)

    def _navigate(self, key: str) -> None:
        if self.on_navigate is not None:
            self.on_navigate(key)
