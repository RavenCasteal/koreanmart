"""
Stage 4 - recipe management. Stage 11a - recipes are now PER DISH.

What this screen does:
  * shows a dish selector at the top (every active dish from menu_items)
  * shows THAT dish's recipe and its ingredient lines, all from SQLite
  * edits how much of each ingredient ONE serving needs
  * adds an active catalog ingredient to the recipe
  * removes a recipe line (with confirmation)
  * edits the recipe's free-text notes

What it deliberately does NOT do:
  * change quantity_on_hand. Recipe amounts (recipe_ingredients) and current
    stock (ingredients) are separate tables. Editing a recipe creates no
    stock movement and never deducts anything.
  * calculate servings or check sufficiency - that is the Calculator screen.

All SQL lives in recipe_repository.py; this file only builds the interface.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable

import customtkinter as ctk

import menu_repository as dishes
import permissions
import recipe_repository
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

# Every row is its own grid, so all rows must share the same minsize and
# uniform group. Otherwise the header and data columns drift apart.
COLUMN_WEIGHTS = (3, 1, 2, 2, 2)
COLUMN_MINSIZES = (200, 70, 140, 140, 140)
COLUMN_TITLES = ("Ingredient", "Unit", "Qty / serving", "On hand", "Actions")


class RecipeView(ctk.CTkFrame):
    """Recipe / bill of materials editor for the dish chosen at the top."""

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
        # Stage 11a: which dish's recipe is being edited. None means "primary
        # POS dish", which keeps the original single-dish behaviour.
        self.selected_id: int | None = None
        self.dish_names: dict[str, int] = {}

        self.scroller = ctk.CTkScrollableFrame(self, fg_color=CREAM, corner_radius=0)
        self.scroller.pack(fill="both", expand=True)
        self.container = ctk.CTkFrame(self.scroller, fg_color="transparent")
        self.container.pack(fill="both", expand=True, padx=26, pady=22)
        self._render()

    def refresh(self) -> None:
        """Header Refresh button re-reads the whole recipe from SQLite."""
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
            options = dishes.list_dishes(
                include_inactive=False, actor=self.user
            )
            self.dish_names = {
                str(row["name"])
                + ("  (POS dish)" if int(row["singleton"]) == 1 else ""):
                int(row["menu_item_id"])
                for row in options
            }
            if self.selected_id not in self.dish_names.values():
                primary = next(
                    (
                        int(row["menu_item_id"])
                        for row in options
                        if int(row["singleton"]) == 1
                    ),
                    None,
                )
                self.selected_id = primary or (
                    int(options[0]["menu_item_id"]) if options else None
                )
            recipe = recipe_repository.get_recipe(
                menu_item_id=self.selected_id
            )
            self.current_dish_name = str(recipe["menu_item_name"])
            lines = recipe_repository.list_recipe_lines(
                menu_item_id=self.selected_id
            )
        except (ValueError, sqlite3.Error, permissions.PermissionDenied) as exc:
            ctk.CTkLabel(
                self.container,
                text=f"Could not load the recipe: {exc}",
                font=font(13),
                text_color=DANGER,
                anchor="w",
            ).pack(anchor="w")
            return

        self._build_dish_picker()
        self._build_top(recipe)
        self._build_summary(recipe)
        self._build_notes(recipe)
        self._build_table(lines)
        self._build_message()
        self._build_footer()

    def _build_dish_picker(self) -> None:
        """Stage 11a: choose which dish's bill of materials to edit."""
        bar = ctk.CTkFrame(
            self.container,
            fg_color=CARD,
            corner_radius=12,
            border_width=1,
            border_color=BORDER,
        )
        bar.pack(fill="x", pady=(0, 14))
        inner = ctk.CTkFrame(bar, fg_color="transparent")
        inner.pack(fill="x", padx=18, pady=12)
        ctk.CTkLabel(
            inner,
            text="Editing recipe for:",
            font=font(12, "bold"),
            text_color=TEXT,
        ).pack(side="left")
        labels = list(self.dish_names)
        self.dish_combo = ctk.CTkComboBox(
            inner,
            values=labels,
            width=270,
            height=34,
            corner_radius=9,
            command=self._on_dish_change,
            fg_color=PANEL,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT,
            font=font(12),
            dropdown_font=font(12),
        )
        self.dish_combo.pack(side="left", padx=(12, 0))
        current = next(
            (
                label
                for label, key in self.dish_names.items()
                if key == self.selected_id
            ),
            labels[0] if labels else "",
        )
        self.dish_combo.set(current)
        ctk.CTkLabel(
            inner,
            text=(
                f"{len(labels)} active dish(es)  ·  ingredient stock is shared "
                "across all dishes"
            ),
            font=font(11),
            text_color=TEXT_MUTED,
            anchor="w",
        ).pack(side="left", padx=(14, 0))

    def _on_dish_change(self, choice: str) -> None:
        key = self.dish_names.get(choice)
        if key is None or key == self.selected_id:
            return
        self.selected_id = key
        self._set_message("")
        self._render()

    def _build_top(self, recipe: dict[str, Any]) -> None:
        top = ctk.CTkFrame(self.container, fg_color="transparent")
        top.pack(fill="x", pady=(0, 14))

        left = ctk.CTkFrame(top, fg_color="transparent")
        left.pack(side="left")
        ctk.CTkLabel(
            left,
            text=str(recipe["menu_item_name"]),
            font=font(20, "bold"),
            text_color=TEXT,
        ).pack(anchor="w")
        ctk.CTkLabel(
            left,
            text="Recipe for one serving (1 bowl)  ·  quantities are editable per dish",
            font=font(11),
            text_color=TEXT_MUTED,
            anchor="w",
        ).pack(anchor="w")

        self.add_button = ctk.CTkButton(
            top,
            text="+  Add ingredient to recipe",
            command=self._open_add_dialog,
            width=210,
            height=38,
            corner_radius=9,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            text_color="#FFFFFF",
            font=font(12, "bold"),
        )
        self.add_button.pack(side="right")

    def _build_summary(self, recipe: dict[str, Any]) -> None:
        cards = ctk.CTkFrame(self.container, fg_color="transparent")
        cards.pack(fill="x", pady=(0, 16))
        for column in range(3):
            cards.grid_columnconfigure(column, weight=1)

        self._stat_card(
            cards, 0, "Recipe ingredients", recipe["ingredient_count"],
            "Ingredient lines in this recipe", ACCENT,
        )
        self._stat_card(
            cards, 1, "Measurement units", recipe["unit_count"],
            "Distinct units from the ingredient catalog", WARNING,
        )
        self._stat_card(
            cards, 2, "Last updated", recipe["updated_at"] or "Never",
            "Recipe definition changes only", SUCCESS, value_size=15,
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
            wraplength=220,
            justify="left",
        ).pack(fill="x", padx=18, pady=(2, 0))
        ctk.CTkLabel(
            card,
            text=note,
            font=font(11),
            text_color=TEXT_MUTED,
            anchor="w",
            wraplength=220,
            justify="left",
        ).pack(fill="x", padx=18, pady=(2, 16))

    def _build_notes(self, recipe: dict[str, Any]) -> None:
        card = ctk.CTkFrame(
            self.container,
            fg_color=CARD,
            corner_radius=14,
            border_width=1,
            border_color=BORDER,
        )
        card.pack(fill="x", pady=(0, 16))

        head = ctk.CTkFrame(card, fg_color="transparent")
        head.pack(fill="x", padx=20, pady=(14, 4))
        ctk.CTkLabel(
            head, text="Recipe notes", font=font(14, "bold"), text_color=TEXT
        ).pack(side="left")
        ctk.CTkButton(
            head,
            text="Edit notes",
            command=self._open_notes_dialog,
            width=96,
            height=28,
            corner_radius=7,
            fg_color=PANEL,
            hover_color="#F0E9DD",
            border_width=1,
            border_color=BORDER,
            text_color=TEXT_MUTED,
            font=font(11),
        ).pack(side="right")

        notes = str(recipe["notes"] or "").strip()
        ctk.CTkLabel(
            card,
            text=notes or "No notes yet. Add preparation reminders for this recipe.",
            font=font(12),
            text_color=TEXT if notes else TEXT_MUTED,
            anchor="w",
            wraplength=780,
            justify="left",
        ).pack(fill="x", padx=20, pady=(0, 16))

    def _build_table(self, lines: list[dict[str, Any]]) -> None:
        panel = ctk.CTkFrame(
            self.container,
            fg_color=CARD,
            corner_radius=14,
            border_width=1,
            border_color=BORDER,
        )
        panel.pack(fill="x")

        header = self._row_frame(panel, PANEL)
        header.pack(fill="x", padx=16, pady=(16, 6))
        for column, title in enumerate(COLUMN_TITLES):
            self._cell(header, column, title, header=True)

        body = ctk.CTkFrame(panel, fg_color="transparent")
        body.pack(fill="x", padx=16)

        if not lines:
            empty = ctk.CTkFrame(body, fg_color=PANEL, corner_radius=10)
            empty.pack(fill="x", pady=(8, 12))
            ctk.CTkLabel(
                empty, text="This recipe has no ingredients yet",
                font=font(14, "bold"), text_color=TEXT,
            ).pack(pady=(18, 2))
            ctk.CTkLabel(
                empty, text="Use 'Add ingredient to recipe' to build it.",
                font=font(11), text_color=TEXT_MUTED,
            ).pack(pady=(0, 18))
        else:
            for line in lines:
                self._recipe_row(body, line)

        ctk.CTkLabel(
            panel,
            text=(
                "Amounts are for ONE serving. 'On hand' is read-only current "
                "stock and is never changed by this screen."
            ),
            font=font(11),
            text_color=TEXT_MUTED,
            anchor="w",
            wraplength=780,
            justify="left",
        ).pack(fill="x", padx=20, pady=(2, 16))

    @staticmethod
    def _row_frame(parent: Any, background: str) -> ctk.CTkFrame:
        row = ctk.CTkFrame(parent, fg_color=background, corner_radius=8)
        for index, weight in enumerate(COLUMN_WEIGHTS):
            row.grid_columnconfigure(
                index,
                weight=weight,
                minsize=COLUMN_MINSIZES[index],
                uniform="recipe_columns",
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
            wraplength=185,
            justify="left",
        ).grid(row=0, column=column, sticky="w", padx=6, pady=7)

    def _recipe_row(self, parent: Any, line: dict[str, Any]) -> None:
        row = self._row_frame(parent, CARD)
        row.pack(fill="x", pady=2)

        name = str(line["name"])
        if line["is_active"] != 1:
            name += "  (inactive)"
        self._cell(
            row, 0, name,
            color=WARNING if line["is_active"] != 1 else None,
        )
        self._cell(row, 1, str(line["unit"]))
        self._cell(
            row, 2,
            format_quantity(line["quantity_per_serving"], line["unit"]),
        )
        self._cell(
            row, 3,
            format_quantity(line["quantity_on_hand"], line["unit"]),
            color=SUCCESS if line["status"] == "In stock" else TEXT_MUTED,
        )

        actions = ctk.CTkFrame(row, fg_color="transparent")
        actions.grid(row=0, column=4, sticky="w", padx=6, pady=5)
        ctk.CTkButton(
            actions,
            text="Edit",
            command=lambda current=line: self._open_edit_dialog(current),
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
            text="Remove",
            command=lambda current=line: self._open_remove_dialog(current),
            width=66,
            height=26,
            corner_radius=7,
            fg_color="transparent",
            hover_color="#F8E5E1",
            border_width=1,
            border_color=BORDER,
            text_color=DANGER,
            font=font(11),
        ).pack(side="left", padx=(6, 0))

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
            "Recipe quantities live in recipe_ingredients. Current stock lives "
            "in ingredients.quantity_on_hand - they are separate on purpose.",
            "Editing, adding or removing a recipe line does NOT deduct stock "
            "and creates no inventory history record. Stock changes only "
            "through stock-in / stock-out transactions on the Inventory screen.",
            "Need a new ingredient? Add it on the Ingredients screen first, "
            "then it becomes available in 'Add ingredient to recipe'.",
            "The Calculator screen reports what is needed for any number of "
            "servings of the POS dish and never deducts stock.",
            "Stage 11a: every dish has its own recipe, but all dishes draw "
            "from the SAME shared ingredient stock. Adding a dish on the "
            "Menu screen creates an empty recipe you can build here.",
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

    # ------------------------------------------------------------------
    # Messages and actions
    # ------------------------------------------------------------------
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

    def _open_edit_dialog(self, line: dict[str, Any]) -> None:
        self._set_message("")
        self.dialog = QuantityDialog(
            self,
            line=line,
            menu_item_id=self.selected_id,
            on_saved=self._after_save,
        )

    def _open_add_dialog(self) -> None:
        self._set_message("")
        try:
            available = recipe_repository.list_available_ingredients(
                menu_item_id=self.selected_id
            )
        except (ValueError, sqlite3.Error) as exc:
            self._set_message(f"Could not load ingredients: {exc}", ok=False)
            return
        self.dialog = AddRecipeIngredientDialog(
            self,
            available=available,
            menu_item_id=self.selected_id,
            on_saved=self._after_save,
            on_navigate=self.on_navigate,
        )

    def _open_remove_dialog(self, line: dict[str, Any]) -> None:
        self._set_message("")
        self.dialog = RemoveRecipeLineDialog(
            self,
            line=line,
            menu_item_id=self.selected_id,
            dish_name=str(
                getattr(self, "current_dish_name", "this dish")
            ),
            on_saved=self._after_save,
        )

    def _open_notes_dialog(self) -> None:
        self._set_message("")
        try:
            notes = recipe_repository.get_recipe(
                menu_item_id=self.selected_id
            )["notes"] or ""
        except (ValueError, sqlite3.Error) as exc:
            self._set_message(f"Could not load notes: {exc}", ok=False)
            return
        self.dialog = NotesDialog(
            self,
            notes=notes,
            menu_item_id=self.selected_id,
            on_saved=self._after_save,
        )


# ----------------------------------------------------------------------
# Modal dialogs
# ----------------------------------------------------------------------
class QuantityDialog(ModalDialog):
    """Edit the amount of one ingredient needed for a single serving."""

    def __init__(
        self,
        master: Any,
        line: dict[str, Any],
        on_saved: Callable[[str], None] | None = None,
        menu_item_id: int | None = None,
    ) -> None:
        super().__init__(master, "Edit recipe quantity", 430)
        self.line = line
        self.on_saved = on_saved
        self.menu_item_id = menu_item_id

        self.heading(
            "Edit per-serving amount",
            f"{line['name']}  ·  measured in {line['unit']}. "
            f"Stock on hand: "
            f"{format_quantity(line['quantity_on_hand'], line['unit'])} "
            "(unchanged).",
        )
        self.quantity_entry = self.field(
            "Quantity needed for 1 serving", on_enter=self._save
        )
        self.quantity_entry.insert(
            0, format_number(line["quantity_per_serving"])
        )
        self.error_label()
        self.actions("Save quantity", self._save)
        self.finish(self.quantity_entry)

    def _save(self) -> None:
        self.show_error("")
        try:
            result = recipe_repository.update_quantity(
                self.line["recipe_ingredient_id"],
                self.quantity_entry.get(),
                menu_item_id=self.menu_item_id,
            )
        except (ValueError, sqlite3.Error) as exc:
            self.show_error(str(exc))
            return
        self.close()
        if self.on_saved:
            self.on_saved(
                f"Updated '{result['name']}' to "
                f"{format_quantity(result['quantity_per_serving'], self.line['unit'])} "
                "per serving. Stock was not changed."
            )


class AddRecipeIngredientDialog(ModalDialog):
    """Pick an active ingredient not yet in the recipe and set its amount."""

    def __init__(
        self,
        master: Any,
        available: list[dict[str, Any]],
        on_saved: Callable[[str], None] | None = None,
        on_navigate: Callable[[str], None] | None = None,
        menu_item_id: int | None = None,
    ) -> None:
        super().__init__(master, "Add ingredient to recipe", 460)
        self.available = available
        self.on_saved = on_saved
        self.on_navigate = on_navigate
        self.menu_item_id = menu_item_id
        self.options: dict[str, int] = {}

        if not available:
            self.heading(
                "Nothing available to add",
                "Every active ingredient is already used in this recipe.",
            )
            ctk.CTkLabel(
                self.card,
                text=(
                    "To add something new, first create it on the Ingredients "
                    "screen. It will then appear in this list."
                ),
                font=font(12),
                text_color=TEXT_MUTED,
                wraplength=390,
                justify="left",
                anchor="w",
            ).pack(anchor="w", padx=24, pady=(0, 14))
            ctk.CTkButton(
                self.card,
                text="Go to Ingredients",
                command=self._go_ingredients,
                height=40,
                corner_radius=10,
                fg_color=ACCENT,
                hover_color=ACCENT_HOVER,
                text_color="#FFFFFF",
                font=font(13, "bold"),
            ).pack(fill="x", padx=24, pady=(4, 8))
            ctk.CTkButton(
                self.card,
                text="Close",
                command=self.close,
                height=34,
                corner_radius=10,
                fg_color="transparent",
                hover_color=PANEL,
                border_width=1,
                border_color=BORDER,
                text_color=TEXT_MUTED,
                font=font(12),
            ).pack(fill="x", padx=24, pady=(0, 22))
            self.finish()
            return

        self.heading(
            "Add an ingredient",
            "Choose from the active ingredient catalog. Each ingredient can "
            "appear in the recipe only once.",
        )
        ctk.CTkLabel(
            self.card, text="Ingredient", font=font(12, "bold"), text_color=TEXT
        ).pack(anchor="w", padx=24, pady=(0, 4))
        for item in available:
            display = f"{item['name']} ({item['unit']})"
            self.options[display] = int(item["ingredient_id"])
        labels = list(self.options)
        self.combo = ctk.CTkComboBox(
            self.card,
            values=labels,
            height=40,
            corner_radius=10,
            fg_color=PANEL,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT,
            font=font(13),
            dropdown_font=font(12),
        )
        self.combo.pack(fill="x", padx=24)
        self.combo.set(labels[0])

        self.quantity_entry = self.field(
            "Quantity needed for 1 serving", on_enter=self._save
        )
        self.error_label()
        self.actions("Add to recipe", self._save)
        self.finish(self.quantity_entry)

    def _go_ingredients(self) -> None:
        self.close()
        if self.on_navigate is not None:
            self.on_navigate("ingredients")

    def _save(self) -> None:
        self.show_error("")
        ingredient_id = self.options.get(self.combo.get())
        if ingredient_id is None:
            self.show_error("Please choose an ingredient from the list.")
            return
        try:
            result = recipe_repository.add_ingredient(
                ingredient_id,
                self.quantity_entry.get(),
                menu_item_id=self.menu_item_id,
            )
        except (ValueError, sqlite3.Error) as exc:
            self.show_error(str(exc))
            return
        self.close()
        if self.on_saved:
            self.on_saved(
                f"Added '{result['name']}' at "
                f"{format_quantity(result['quantity_per_serving'], result['unit'])} "
                "per serving."
            )


class RemoveRecipeLineDialog(ModalDialog):
    """Confirmation before deleting ONE recipe line."""

    def __init__(
        self,
        master: Any,
        line: dict[str, Any],
        on_saved: Callable[[str], None] | None = None,
        menu_item_id: int | None = None,
        dish_name: str = "this dish",
    ) -> None:
        super().__init__(master, "Remove recipe ingredient", 440)
        self.line = line
        self.on_saved = on_saved
        self.menu_item_id = menu_item_id
        self.dish_name = dish_name

        self.heading(
            "Remove from this recipe?",
            f"'{line['name']}' currently needs "
            f"{format_quantity(line['quantity_per_serving'], line['unit'])} "
            "per serving.",
        )
        for text in (
            "Only this recipe line is deleted.",
            "The ingredient stays in the catalog and keeps its stock balance.",
            "No inventory history record is created.",
        ):
            ctk.CTkLabel(
                self.card,
                text="·  " + text,
                font=font(11),
                text_color=TEXT_MUTED,
                wraplength=380,
                justify="left",
                anchor="w",
            ).pack(anchor="w", padx=24, pady=2)

        # No spacer widget: an empty packed frame/label draws a faint seam
        # across the card on Windows. Padding does the same job cleanly.
        self.error_label(height=28)
        self.actions(
            "Remove from recipe", self._confirm, danger=True
        )
        self.finish()

    def _confirm(self) -> None:
        self.show_error("")
        try:
            result = recipe_repository.remove_ingredient(
                self.line["recipe_ingredient_id"],
                menu_item_id=self.menu_item_id,
            )
        except (ValueError, sqlite3.Error) as exc:
            self.show_error(str(exc))
            return
        self.close()
        if self.on_saved:
            self.on_saved(
                f"Removed '{result['name']}' from the {self.dish_name} recipe. "
                "Stock and history were not changed."
            )


class NotesDialog(ModalDialog):
    """Edit the recipe's free-text notes."""

    def __init__(
        self,
        master: Any,
        notes: str,
        on_saved: Callable[[str], None] | None = None,
        menu_item_id: int | None = None,
    ) -> None:
        super().__init__(master, "Recipe notes", 460)
        self.on_saved = on_saved
        self.menu_item_id = menu_item_id

        self.heading(
            "Recipe notes",
            f"Optional reminders for preparing one bowl. "
            f"Maximum {recipe_repository.MAX_NOTES_LENGTH} characters.",
        )
        self.textbox = ctk.CTkTextbox(
            self.card,
            height=110,
            corner_radius=10,
            fg_color=PANEL,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT,
            font=font(12),
        )
        self.textbox.pack(fill="x", padx=24)
        self.textbox.insert("0.0", str(notes or ""))
        self.error_label()
        self.actions("Save notes", self._save)
        self.finish(self.textbox)

    def _save(self) -> None:
        self.show_error("")
        try:
            clean = recipe_repository.update_notes(
                self.textbox.get("0.0", "end"),
                menu_item_id=self.menu_item_id,
            )
        except (ValueError, sqlite3.Error) as exc:
            self.show_error(str(exc))
            return
        self.close()
        if self.on_saved:
            self.on_saved(
                "Recipe notes saved." if clean else "Recipe notes cleared."
            )
