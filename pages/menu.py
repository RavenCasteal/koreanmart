"""
Stage 11a - Menu / dishes screen (MANAGER ONLY).

A manager can add a new sellable dish, edit its name / description / price
in Philippine peso, and deactivate or reactivate it. Every new dish gets its
own recipe (bill of materials), which is edited on the Recipe screen by
choosing the dish from the dropdown at the top of that page.

What Stage 11a deliberately does NOT do:
  * sell the new dish on the POS screen yet - that is Stage 11b. Exactly one
    "POS dish" (Regular Ramen) remains, so the existing sales code, receipts
    and 365-test regression suite stay untouched.
  * delete a dish. Deactivation is a status change, so past sales snapshots
    and recipe history remain valid.
  * touch physical stock. Adding a dish only creates a menu_items row and an
    empty recipes row; no ingredient balance or stock movement is written.

All SQL lives in menu_repository.py; this file only builds the interface.
"""

from __future__ import annotations

import sqlite3
from tkinter import messagebox
from typing import Any, Callable

import customtkinter as ctk

import menu_repository as dishes
import permissions
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

TABLE_WEIGHTS = (3, 2, 2, 2, 2, 4)
TABLE_MINSIZES = (170, 115, 110, 105, 125, 230)
TABLE_TITLES = ("Dish", "Unit price", "Recipe lines", "Status", "Role", "Actions")


class DishDialog(ModalDialog):
    """Add a dish or edit an existing one. Prices are always PHP."""

    def __init__(
        self,
        master: Any,
        actor: Any,
        on_saved: Callable[[str], None],
        target: dict[str, Any] | None = None,
    ) -> None:
        editing = target is not None
        super().__init__(
            master, "Edit dish" if editing else "Add dish", 470
        )
        self.actor = actor
        self.target = target
        self.on_saved = on_saved

        self.heading(
            "Edit dish" if editing else "Add a new dish",
            "Each dish gets its own recipe. Prices are in Philippine peso "
            "(PHP). Adding a dish never changes ingredient stock.",
        )
        self.name = self.field("Dish name", on_enter=self._save)
        self.description = self.field("Description (optional)", on_enter=self._save)
        self.price = self.field(
            f"Unit price ({CURRENCY_SYMBOL})", on_enter=self._save
        )
        if editing:
            self.name.insert(0, str(target["name"]))
            self.description.insert(0, str(target["description"] or ""))
            self.price.insert(0, format_number(target["unit_price"]))
            if int(target["singleton"]) == 1:
                # The POS screen still prints "Regular Ramen" in Stage 11a.
                self.name.configure(state="disabled")
        else:
            self.price.insert(0, "150.00")
        self.error_label()
        self.actions(
            "Save dish" if editing else "Add dish", self._save
        )
        self.finish(self.price if editing else self.name)

    def _save(self) -> None:
        self.show_error("")
        try:
            name = (
                str(self.target["name"])
                if self.target is not None and int(self.target["singleton"]) == 1
                else self.name.get()
            )
            description = self.description.get()
            price = self.price.get()
            if self.target is None:
                dishes.add_dish(
                    name, description, price, actor=self.actor
                )
                message = (
                    f"'{dishes.validate_name(name)}' was added with its own "
                    "empty recipe. Add ingredients on the Recipe screen."
                )
            else:
                result = dishes.update_dish(
                    self.target["menu_item_id"],
                    name,
                    description,
                    price,
                    actor=self.actor,
                )
                message = f"'{result['name']}' was updated."
        except (ValueError, sqlite3.Error, permissions.PermissionDenied) as exc:
            self.show_error(str(exc))
            return
        self.close()
        self.on_saved(message)


class MenuView(ctk.CTkFrame):
    """Manager-only dish list with add / edit / deactivate actions."""

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
        # Third line of defence: even a direct instantiation shows no data
        # and no action buttons to a cashier.
        self.allowed = permissions.can(
            self.actor, permissions.CAP_MANAGE_MENU
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
            text="Menu and recipe management are visible only to a manager. "
                 "Please sign in with a manager account.",
            font=font(12),
            text_color=TEXT_MUTED,
            anchor="w",
            wraplength=760,
            justify="left",
        ).pack(anchor="w")

    def _build_toolbar(self) -> None:
        top = ctk.CTkFrame(self.container, fg_color="transparent")
        top.pack(fill="x", pady=(0, 12))
        left = ctk.CTkFrame(top, fg_color="transparent")
        left.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(
            left, text="Menu / dishes", font=font(20, "bold"), text_color=TEXT
        ).pack(anchor="w")
        ctk.CTkLabel(
            left,
            text="Manager only  ·  each dish has its own recipe and PHP price",
            font=font(11),
            text_color=TEXT_MUTED,
            anchor="w",
        ).pack(anchor="w")
        buttons = ctk.CTkFrame(top, fg_color="transparent")
        buttons.pack(side="right")
        ctk.CTkButton(
            buttons,
            text="Recipe screen",
            command=self._go_recipe,
            width=120,
            height=34,
            corner_radius=8,
            fg_color=PANEL,
            hover_color="#F0E9DD",
            border_width=1,
            border_color=BORDER,
            text_color=TEXT_MUTED,
            font=font(11),
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            buttons,
            text="+  Add dish",
            command=self._add_dish,
            width=120,
            height=34,
            corner_radius=8,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            text_color="#FFFFFF",
            font=font(12, "bold"),
        ).pack(side="left")
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
        self.status_label.pack(fill="x", pady=(10, 4))

    def _build_table(self) -> None:
        panel = ctk.CTkFrame(
            self.container,
            fg_color=CARD,
            corner_radius=14,
            border_width=1,
            border_color=BORDER,
        )
        panel.pack(fill="x")
        header = self._grid_row(panel, PANEL)
        header.pack(fill="x", padx=16, pady=(16, 6))
        for column, title in enumerate(TABLE_TITLES):
            self._cell(header, column, title, header=True)
        self.rows_frame = ctk.CTkFrame(panel, fg_color="transparent")
        self.rows_frame.pack(fill="x", padx=16)
        self.table_note = ctk.CTkLabel(
            panel,
            text="",
            font=font(11),
            text_color=TEXT_MUTED,
            anchor="w",
            wraplength=820,
            justify="left",
        )
        self.table_note.pack(fill="x", padx=20, pady=(0, 16))

    def _build_notes(self) -> None:
        notes = (
            "Only the dish marked POS dish can currently be sold. Stage 11b "
            "will add a dish picker to the Point of Sale screen.",
            "A dish is deactivated, never deleted. Past receipts keep their "
            "own snapshot of the dish name, price and deducted ingredients.",
            "Recipe amounts live in recipe_ingredients; physical stock lives "
            "in ingredients.quantity_on_hand. Both are shared across dishes "
            "and are never changed from this screen.",
            "To build a new dish: add it here, then open the Recipe screen "
            "and choose it from the dish dropdown at the top.",
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
        """Header Refresh button re-reads every dish from SQLite."""
        if not self.allowed:
            return
        try:
            rows = dishes.list_dishes(actor=self.actor)
            counts = dishes.summary(actor=self.actor)
        except (ValueError, sqlite3.Error, permissions.PermissionDenied) as exc:
            self._message(str(exc), error=True)
            return
        self._render_summary(counts, rows)
        self._render_rows(rows)

    def _render_summary(
        self, counts: dict[str, Any], rows: list[dict[str, Any]]
    ) -> None:
        for child in self.summary_frame.winfo_children():
            child.destroy()
        primary = next(
            (row for row in rows if int(row["singleton"]) == 1), None
        )
        cards = (
            ("Total dishes", str(counts["total"]), "Including inactive", TEXT_MUTED),
            ("Active dishes", str(counts["active"]), "Available in the app", SUCCESS),
            ("Recipes built", str(counts["with_recipe"]), "Dishes with ingredients", ACCENT),
            (
                "Current POS dish",
                str(primary["name"]) if primary else "None",
                format_money(primary["unit_price"]) if primary else "—",
                WARNING,
            ),
        )
        for column, (label, value, note, accent) in enumerate(cards):
            self.summary_frame.grid_columnconfigure(column, weight=1)
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
                text=value,
                font=font(17 if column == 3 else 21, "bold"),
                text_color=TEXT,
                anchor="w",
                wraplength=210,
                justify="left",
            ).pack(fill="x", padx=18, pady=(2, 0))
            ctk.CTkLabel(
                card, text=note, font=font(11), text_color=TEXT_MUTED, anchor="w"
            ).pack(fill="x", padx=18, pady=(2, 16))

    def _render_rows(self, rows: list[dict[str, Any]]) -> None:
        for child in self.rows_frame.winfo_children():
            child.destroy()
        for item in rows:
            row = self._grid_row(self.rows_frame, CARD)
            row.pack(fill="x", pady=2)
            active = bool(item["is_active"])
            primary = int(item["singleton"]) == 1
            self._cell(
                row, 0, str(item["name"]),
                color=SUCCESS if active else TEXT_MUTED,
            )
            self._cell(row, 1, format_money(item["unit_price"]))
            lines = int(item["ingredient_count"] or 0)
            self._cell(
                row, 2,
                f"{lines} line(s)" if lines else "Not built yet",
                color=TEXT_MUTED if not lines else None,
            )
            self._cell(
                row, 3, "Active" if active else "Inactive",
                color=SUCCESS if active else DANGER,
            )
            self._cell(
                row, 4, dishes.PRIMARY_LABEL if primary else "Additional dish",
                color=WARNING if primary else TEXT_MUTED,
            )
            actions = ctk.CTkFrame(row, fg_color="transparent")
            actions.grid(row=0, column=5, sticky="w", padx=4, pady=5)
            ctk.CTkButton(
                actions,
                text="Edit",
                command=lambda target=item: self._edit_dish(target),
                width=54,
                height=24,
                corner_radius=6,
                fg_color=PANEL,
                hover_color="#F0E9DD",
                border_width=1,
                border_color=BORDER,
                text_color=TEXT,
                font=font(10),
            ).pack(side="left", padx=(0, 4))
            if not primary:
                ctk.CTkButton(
                    actions,
                    text="Deactivate" if active else "Reactivate",
                    command=lambda target=item, flag=active: self._toggle(
                        target, flag
                    ),
                    width=94,
                    height=24,
                    corner_radius=6,
                    fg_color=DANGER if active else SUCCESS,
                    hover_color="#8F2D21" if active else "#326349",
                    text_color="#FFFFFF",
                    font=font(10),
                ).pack(side="left")
            else:
                ctk.CTkLabel(
                    actions,
                    text="Always active",
                    font=font(10),
                    text_color=TEXT_MUTED,
                ).pack(side="left")
        self.table_note.configure(
            text=(
                "Showing " + str(len(rows)) + " dish(es). Prices are "
                "Philippine peso (PHP). Only the POS dish is sellable in "
                "Stage 11a."
            )
        )

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def _go_recipe(self) -> None:
        if self.on_navigate is not None:
            self.on_navigate("recipe")

    def _after_save(self, message: str) -> None:
        self._message(message)
        self.refresh()

    def _add_dish(self) -> None:
        self._message("")
        self.dialog = DishDialog(self, self.actor, self._after_save)

    def _edit_dish(self, target: dict[str, Any]) -> None:
        self._message("")
        try:
            full = dishes.get_dish(target["menu_item_id"], actor=self.actor)
        except (ValueError, sqlite3.Error, permissions.PermissionDenied) as exc:
            self._message(str(exc), error=True)
            return
        self.dialog = DishDialog(self, self.actor, self._after_save, full)

    def _toggle(
        self, target: dict[str, Any], currently_active: bool
    ) -> None:
        if currently_active:
            answer = messagebox.askyesno(
                "Deactivate dish",
                "Deactivate '" + str(target["name"]) + "'?\n\n"
                "It stays in the database with its recipe and history, but "
                "is hidden from the active-dish lists.",
            )
            if not answer:
                return
        try:
            dishes.set_active(
                target["menu_item_id"], not currently_active, actor=self.actor
            )
        except (ValueError, sqlite3.Error, permissions.PermissionDenied) as exc:
            self._message(str(exc), error=True)
            return
        self._message(
            "'" + str(target["name"]) + "' "
            + ("deactivated." if currently_active else "reactivated.")
        )
        self.refresh()

    # ------------------------------------------------------------------
    # Shared row helpers (same uniform columns for header and data rows)
    # ------------------------------------------------------------------
    @staticmethod
    def _grid_row(parent: Any, background: str) -> ctk.CTkFrame:
        row = ctk.CTkFrame(parent, fg_color=background, corner_radius=8)
        for index, weight in enumerate(TABLE_WEIGHTS):
            row.grid_columnconfigure(
                index,
                weight=weight,
                minsize=TABLE_MINSIZES[index],
                uniform="menu_table",
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
