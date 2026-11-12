"""
Stage 3 - Ingredient management.

Full CRUD over the ingredients table:
  * Add / edit / soft-delete ingredients
  * Live search by name or unit (SQL LIKE, backed by SQLite)
  * Available quantity is displayed READ-ONLY (only stock-in/out may change it)
  * Minimum stock, measurement unit and expiration date are editable
  * Expiration dates are colour-coded as expired / near expiry / OK

Every read and write goes through repository.py; this file contains no SQL.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable

import customtkinter as ctk

import repository
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

# Column weights keep the table aligned even when the window is resized.
# Every row is its own grid, so each column also needs the SAME minsize and
# uniform group; otherwise the header and data columns drift apart.
COLUMN_WEIGHTS = (3, 1, 2, 1, 2, 2, 2)
COLUMN_MINSIZES = (170, 58, 108, 68, 108, 108, 118)
COLUMN_TITLES = (
    "Ingredient", "Unit", "On hand", "Minimum", "Expires", "Status", "Actions"
)


class IngredientsView(ctk.CTkFrame):
    """Searchable ingredient catalog with working add/edit/delete dialogs."""

    def __init__(
        self,
        master: Any,
        user: dict[str, Any] | None = None,
        on_navigate: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(master, fg_color=CREAM, corner_radius=0)
        self.user = user
        self.on_navigate = on_navigate
        self.dialog: ctk.CTkToplevel | None = None

        self.scroller = ctk.CTkScrollableFrame(self, fg_color=CREAM, corner_radius=0)
        self.scroller.pack(fill="both", expand=True)

        self.container = ctk.CTkFrame(self.scroller, fg_color="transparent")
        self.container.pack(fill="both", expand=True, padx=26, pady=22)

        self._build_toolbar()
        self._build_table()
        self._build_footer()
        self._render_rows()
        self._set_message("")

    # ------------------------------------------------------------------
    # Static layout (built once so typing in the search box keeps focus)
    # ------------------------------------------------------------------
    def _build_toolbar(self) -> None:
        top = ctk.CTkFrame(self.container, fg_color="transparent")
        top.pack(fill="x", pady=(0, 12))

        left = ctk.CTkFrame(top, fg_color="transparent")
        left.pack(side="left")
        ctk.CTkLabel(
            left, text="Ingredient catalog", font=font(20, "bold"), text_color=TEXT
        ).pack(anchor="w")
        self.count_label = ctk.CTkLabel(
            left, text="", font=font(11), text_color=TEXT_MUTED, anchor="w"
        )
        self.count_label.pack(anchor="w")

        right = ctk.CTkFrame(top, fg_color="transparent")
        right.pack(side="right")
        self.add_button = ctk.CTkButton(
            right,
            text="+  Add ingredient",
            command=self._open_add_dialog,
            width=160,
            height=38,
            corner_radius=9,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            text_color="#FFFFFF",
            font=font(12, "bold"),
        )
        self.add_button.pack(side="right", padx=(10, 0))

        self.clear_button = ctk.CTkButton(
            right,
            text="Clear",
            command=self._clear_search,
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

        self.search_entry = ctk.CTkEntry(
            right,
            placeholder_text="Search by name or unit…",
            width=230,
            height=38,
            corner_radius=9,
            fg_color=CARD,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT,
            font=font(12),
        )
        self.search_entry.pack(side="right", padx=(0, 8))
        self.search_entry.bind("<KeyRelease>", lambda _event: self._render_rows())

    def _build_table(self) -> None:
        self.panel = ctk.CTkFrame(
            self.container,
            fg_color=CARD,
            corner_radius=14,
            border_width=1,
            border_color=BORDER,
        )
        self.panel.pack(fill="x")

        header = self._row_frame(self.panel, PANEL)
        header.pack(fill="x", padx=16, pady=(16, 6))
        for column, title in enumerate(COLUMN_TITLES):
            self._cell(header, column, title, header=True)

        # Only this frame is rebuilt while searching / refreshing.
        self.rows_frame = ctk.CTkFrame(self.panel, fg_color="transparent")
        self.rows_frame.pack(fill="x", padx=16, pady=(0, 10))

    def _build_footer(self) -> None:
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

        ctk.CTkLabel(
            self.container,
            text=(
                "Available quantity is read-only here. Stock can only change through "
                "stock-in / stock-out transactions on the Inventory screen."
            ),
            font=font(11),
            text_color=TEXT_MUTED,
            anchor="w",
            wraplength=820,
            justify="left",
        ).pack(anchor="w")
        ctk.CTkLabel(
            self.container,
            text=(
                "Expiry colours:  red = expired  ·  amber = within "
                f"{repository.EXPIRY_WARNING_DAYS} days  ·  "
                "green = OK. Removed ingredients are kept in SQLite so "
                "transaction history stays intact."
            ),
            font=font(11),
            text_color=TEXT_MUTED,
            anchor="w",
            wraplength=820,
            justify="left",
        ).pack(anchor="w", pady=(2, 8))

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    @staticmethod
    def _row_frame(parent: Any, background: str) -> ctk.CTkFrame:
        row = ctk.CTkFrame(parent, fg_color=background, corner_radius=8)
        for index, weight in enumerate(COLUMN_WEIGHTS):
            row.grid_columnconfigure(
                index,
                weight=weight,
                minsize=COLUMN_MINSIZES[index],
                uniform="ingredient_columns",
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
            wraplength=150,
            justify="left",
        ).grid(row=0, column=column, sticky="w", padx=6, pady=7)

    def _render_rows(self) -> None:
        """Re-read matching rows from SQLite and rebuild only the table body."""
        for child in self.rows_frame.winfo_children():
            child.destroy()

        try:
            rows = repository.list_ingredients(self.search_entry.get())
            total = repository.count_active()
        except sqlite3.Error as exc:
            self._set_message(f"Could not load ingredients: {exc}", ok=False)
            return

        self.count_label.configure(
            text=f"{len(rows)} shown  ·  {total} registered"
        )

        if not rows:
            empty = ctk.CTkFrame(self.rows_frame, fg_color=PANEL, corner_radius=10)
            empty.pack(fill="x", pady=(8, 12))
            ctk.CTkLabel(
                empty,
                text="No ingredients match your search",
                font=font(14, "bold"),
                text_color=TEXT,
            ).pack(pady=(18, 2))
            ctk.CTkLabel(
                empty,
                text="Clear the search box, or add a new ingredient.",
                font=font(11),
                text_color=TEXT_MUTED,
            ).pack(pady=(0, 18))
            return

        for item in rows:
            self._ingredient_row(item)

    def _ingredient_row(self, item: dict[str, Any]) -> None:
        row = self._row_frame(self.rows_frame, CARD)
        row.pack(fill="x", pady=2)

        self._cell(row, 0, str(item["name"]))
        self._cell(row, 1, str(item["unit"]))
        self._cell(
            row, 2,
            format_quantity(item["quantity_on_hand"], item["unit"]),
        )
        self._cell(row, 3, format_number(item["minimum_stock"]))

        expiry = item["expiration_date"]
        expiry_colour = {
            "Expired": DANGER,
            "Near expiry": WARNING,
            "OK": SUCCESS,
        }.get(item["expiry_status"], TEXT_MUTED)
        self._cell(
            row, 4, str(expiry or "—"), color=expiry_colour
        )

        status = item["status"]
        status_colour = (
            DANGER if status == "Out of stock"
            else WARNING if status == "Low stock"
            else SUCCESS
        )
        ctk.CTkLabel(
            row,
            text=status,
            fg_color=status_colour,
            text_color="#FFFFFF",
            corner_radius=6,
            width=92,
            height=22,
            font=font(11, "bold"),
        ).grid(row=0, column=5, sticky="w", padx=6, pady=7)

        actions = ctk.CTkFrame(row, fg_color="transparent")
        actions.grid(row=0, column=6, sticky="w", padx=6, pady=5)
        ctk.CTkButton(
            actions,
            text="Edit",
            command=lambda current=item: self._open_edit_dialog(current),
            width=48,
            height=26,
            corner_radius=7,
            fg_color=PANEL,
            hover_color="#F0E9DD",
            border_width=1,
            border_color=BORDER,
            text_color=TEXT,
            font=font(11),
        ).pack(side="left")
        ctk.CTkButton(
            actions,
            text="Delete",
            command=lambda current=item: self._open_delete_dialog(current),
            width=56,
            height=26,
            corner_radius=7,
            fg_color="transparent",
            hover_color="#F8E5E1",
            border_width=1,
            border_color=BORDER,
            text_color=DANGER,
            font=font(11),
        ).pack(side="left", padx=(6, 0))

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def refresh(self) -> None:
        """Header Refresh button re-reads the catalog from SQLite."""
        self._render_rows()

    def _clear_search(self) -> None:
        self.search_entry.delete(0, "end")
        self._render_rows()
        self.search_entry.focus_set()

    def _set_message(self, text: str, ok: bool = True) -> None:
        self.message_label.configure(
            text=text, text_color=SUCCESS if ok else DANGER
        )

    def _open_add_dialog(self) -> None:
        self._set_message("")
        self.dialog = IngredientDialog(
            self, ingredient=None, on_saved=self._after_save
        )

    def _open_edit_dialog(self, item: dict[str, Any]) -> None:
        self._set_message("")
        self.dialog = IngredientDialog(
            self, ingredient=item, on_saved=self._after_save
        )

    def _after_save(self, message: str) -> None:
        self._set_message(message)
        self.refresh()

    def _open_delete_dialog(self, item: dict[str, Any]) -> None:
        self._set_message("")
        self.dialog = DeleteConfirmDialog(
            self,
            item=item,
            recipe_links=repository.count_recipe_links(item["ingredient_id"]),
            on_confirm=lambda: self._perform_delete(item["ingredient_id"]),
        )

    def _perform_delete(self, ingredient_id: int) -> None:
        try:
            result = repository.delete_ingredient(ingredient_id)
        except (ValueError, sqlite3.Error) as exc:
            self._set_message(f"Could not delete: {exc}", ok=False)
            return

        message = f"Removed '{result['name']}' from the active catalog."
        if result["recipe_links_removed"]:
            message += (
                f" {result['recipe_links_removed']} recipe line(s) were also "
                "removed from Regular Ramen."
            )
        if result["quantity_on_hand"] > 0:
            message += " Its stock balance is preserved in SQLite history."
        self._set_message(message)
        self.refresh()


# ----------------------------------------------------------------------
# Modal dialogs
# ----------------------------------------------------------------------
class IngredientDialog(ctk.CTkToplevel):
    """Add / edit form. Available quantity is deliberately not editable."""

    def __init__(
        self,
        master: Any,
        ingredient: dict[str, Any] | None,
        on_saved: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(master)
        self.ingredient = ingredient
        self.on_saved = on_saved
        self.is_edit = ingredient is not None

        self.title("Edit ingredient" if self.is_edit else "Add ingredient")
        self.resizable(False, False)
        self.configure(fg_color=CREAM)
        self.transient(master.winfo_toplevel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        card = ctk.CTkFrame(
            self,
            fg_color=CARD,
            corner_radius=18,
            border_width=1,
            border_color=BORDER,
        )
        card.pack(fill="both", expand=True, padx=24, pady=24)

        ctk.CTkLabel(
            card,
            text="Edit ingredient" if self.is_edit else "New ingredient",
            font=font(20, "bold"),
            text_color=TEXT,
        ).pack(anchor="w", padx=24, pady=(24, 2))
        ctk.CTkLabel(
            card,
            text=(
                "Update catalog details. Stock on hand is managed separately."
                if self.is_edit
                else "New ingredients always start with zero stock."
            ),
            font=font(11),
            text_color=TEXT_MUTED,
            anchor="w",
        ).pack(anchor="w", padx=24, pady=(0, 18))

        self.name_entry = self._field(card, "Ingredient name")
        self.name_entry.insert(0, ingredient["name"] if self.is_edit else "")

        ctk.CTkLabel(
            card, text="Measurement unit", font=font(12, "bold"), text_color=TEXT
        ).pack(anchor="w", padx=24, pady=(12, 4))
        self.unit_combo = ctk.CTkComboBox(
            card,
            values=repository.get_unit_choices(),
            height=40,
            corner_radius=10,
            fg_color=PANEL,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT,
            font=font(13),
            dropdown_font=font(12),
        )
        self.unit_combo.pack(fill="x", padx=24)
        self.unit_combo.set(ingredient["unit"] if self.is_edit else "grams")

        self.min_entry = self._field(card, "Minimum stock level")
        self.min_entry.insert(
            0,
            format_number(ingredient["minimum_stock"])
            if self.is_edit else "0",
        )

        self.expiry_entry = self._field(card, "Expiration date (YYYY-MM-DD, optional)")
        if self.is_edit and ingredient["expiration_date"]:
            self.expiry_entry.insert(0, str(ingredient["expiration_date"]))

        balance = (
            ingredient["quantity_on_hand"] if self.is_edit else 0
        )
        unit = ingredient["unit"] if self.is_edit else "—"
        ctk.CTkLabel(
            card,
            text=(
                f"Available quantity:  "
                f"{format_quantity(balance, unit)}   (read-only)"
            ),
            font=font(11, "bold"),
            text_color=TEXT_MUTED,
            anchor="w",
        ).pack(anchor="w", padx=24, pady=(14, 0))

        self.error_label = ctk.CTkLabel(
            card,
            text="",
            font=font(11),
            text_color=DANGER,
            wraplength=350,
            justify="left",
            anchor="w",
            height=32,
        )
        self.error_label.pack(fill="x", padx=24, pady=(8, 0))

        ctk.CTkButton(
            card,
            text="Save ingredient" if self.is_edit else "Add ingredient",
            command=self._save,
            height=42,
            corner_radius=10,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            text_color="#FFFFFF",
            font=font(13, "bold"),
        ).pack(fill="x", padx=24, pady=(4, 8))
        ctk.CTkButton(
            card,
            text="Cancel",
            command=self._cancel,
            height=34,
            corner_radius=10,
            fg_color="transparent",
            hover_color=PANEL,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT_MUTED,
            font=font(12),
        ).pack(fill="x", padx=24, pady=(0, 22))

        self._center()
        self.grab_set()
        self.name_entry.focus_set()

    def _field(self, parent: Any, label: str) -> ctk.CTkEntry:
        ctk.CTkLabel(
            parent, text=label, font=font(12, "bold"), text_color=TEXT
        ).pack(anchor="w", padx=24, pady=(12, 4))
        entry = ctk.CTkEntry(
            parent,
            height=40,
            corner_radius=10,
            fg_color=PANEL,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT,
            font=font(13),
        )
        entry.pack(fill="x", padx=24)
        entry.bind("<Return>", lambda _event: self._save())
        return entry

    def _center(self) -> None:
        self.update_idletasks()
        width, height = 440, self.winfo_reqheight() + 48
        root = self.master.winfo_toplevel()
        x = root.winfo_x() + (root.winfo_width() - width) // 2
        y = root.winfo_y() + (root.winfo_height() - height) // 2
        self.geometry(f"{width}x{height}+{max(x, 0)}+{max(y, 0)}")

    def _save(self) -> None:
        self.error_label.configure(text="")
        name = self.name_entry.get()
        unit = self.unit_combo.get()
        minimum = self.min_entry.get()
        expiry = self.expiry_entry.get()
        try:
            clean = repository.validate(name, unit, minimum, expiry)
            values = (
                clean["name"], clean["unit"],
                clean["minimum_stock"], clean["expiration_date"],
            )
            if self.is_edit:
                repository.update_ingredient(
                    self.ingredient["ingredient_id"], *values
                )
                message = f"Updated '{clean['name']}'."
            else:
                repository.add_ingredient(*values)
                message = f"Added '{clean['name']}'."
        except (ValueError, sqlite3.Error) as exc:
            self.error_label.configure(text=str(exc))
            return

        self.grab_release()
        self.destroy()
        if self.on_saved:
            self.on_saved(message)

    def _cancel(self) -> None:
        self.grab_release()
        self.destroy()


class DeleteConfirmDialog(ctk.CTkToplevel):
    """Styled confirmation before a soft delete."""

    def __init__(
        self,
        master: Any,
        item: dict[str, Any],
        recipe_links: int,
        on_confirm: Callable[[], None],
    ) -> None:
        super().__init__(master)
        self.on_confirm = on_confirm

        self.title("Remove ingredient")
        self.resizable(False, False)
        self.configure(fg_color=CREAM)
        self.transient(master.winfo_toplevel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        card = ctk.CTkFrame(
            self,
            fg_color=CARD,
            corner_radius=18,
            border_width=1,
            border_color=BORDER,
        )
        card.pack(fill="both", expand=True, padx=24, pady=24)

        ctk.CTkLabel(
            card, text="Remove this ingredient?", font=font(19, "bold"),
            text_color=TEXT,
        ).pack(anchor="w", padx=24, pady=(24, 8))

        details = [f"'{item['name']}' will no longer appear in the active catalog."]
        if recipe_links:
            details.append(
                f"It is used in {recipe_links} recipe line(s), which will also be "
                "removed from Regular Ramen."
            )
        if item["quantity_on_hand"] > 0:
            details.append(
                "Its stock balance and any transaction history are kept in SQLite."
            )
        details.append("This is a soft delete: the record is not physically erased.")

        for line in details:
            ctk.CTkLabel(
                card,
                text="·  " + line,
                font=font(11),
                text_color=TEXT_MUTED,
                wraplength=350,
                justify="left",
                anchor="w",
            ).pack(anchor="w", padx=24, pady=2)

        # No spacer widget: an empty packed widget draws a faint seam across
        # the card on Windows. Extra padding on the button does the same job.
        ctk.CTkButton(
            card,
            text="Remove ingredient",
            command=self._confirm,
            height=40,
            corner_radius=10,
            fg_color=DANGER,
            hover_color="#8F2D21",
            text_color="#FFFFFF",
            font=font(13, "bold"),
        ).pack(fill="x", padx=24, pady=(16, 8))
        ctk.CTkButton(
            card,
            text="Cancel",
            command=self._cancel,
            height=34,
            corner_radius=10,
            fg_color="transparent",
            hover_color=PANEL,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT_MUTED,
            font=font(12),
        ).pack(fill="x", padx=24, pady=(0, 22))

        self.update_idletasks()
        width, height = 420, self.winfo_reqheight() + 48
        root = master.winfo_toplevel()
        x = root.winfo_x() + (root.winfo_width() - width) // 2
        y = root.winfo_y() + (root.winfo_height() - height) // 2
        self.geometry(f"{width}x{height}+{max(x, 0)}+{max(y, 0)}")
        self.grab_set()

    def _confirm(self) -> None:
        self.grab_release()
        self.destroy()
        self.on_confirm()

    def _cancel(self) -> None:
        self.grab_release()
        self.destroy()
