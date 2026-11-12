"""
Stage 1 - Administrator login screen and first-run password change.

LoginView          : username/password form backed by SQLite users table.
ChangePasswordDialog: modal dialog used on first login (forced) and later
                      from the session screen (optional).
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable

import customtkinter as ctk

import database
from components.theme import (
    ACCENT,
    ACCENT_HOVER,
    BORDER,
    CARD,
    CHARCOAL,
    CREAM,
    DANGER,
    PANEL,
    TEXT,
    TEXT_MUTED,
    TEXT_ON_DARK,
    font,
)


class LoginView(ctk.CTkFrame):
    """Login form. Calls on_success(user_dict) or on_exit()."""

    def __init__(
        self,
        master: Any,
        on_success: Callable[[dict[str, Any]], None],
        on_exit: Callable[[], None],
    ) -> None:
        super().__init__(master, fg_color=CREAM, corner_radius=0)
        self.on_success = on_success
        self.on_exit = on_exit
        self._busy = False
        self._password_visible = False

        self.grid_columnconfigure(0, minsize=370, weight=0)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._build_brand_panel()
        self._build_form_panel()
        self.username_entry.focus_set()

    # ------------------------------------------------------------------
    def _build_brand_panel(self) -> None:
        brand = ctk.CTkFrame(self, fg_color=CHARCOAL, corner_radius=0)
        brand.grid(row=0, column=0, sticky="nsew")

        inner = ctk.CTkFrame(brand, fg_color="transparent")
        inner.place(relx=0.5, rely=0.46, anchor="center")

        ctk.CTkLabel(
            inner,
            text="SINGLE MENU ITEM",
            fg_color=ACCENT,
            corner_radius=6,
            text_color="#FFFFFF",
            font=font(11, "bold"),
            width=140,
            height=26,
        ).pack(anchor="w", pady=(0, 28))

        ctk.CTkLabel(
            inner,
            text="Ramen\nInventory\nSystem",
            font=font(38, "bold"),
            text_color=TEXT_ON_DARK,
            justify="left",
            anchor="w",
        ).pack(anchor="w")

        ctk.CTkFrame(
            inner, fg_color=ACCENT, width=54, height=3, corner_radius=2
        ).pack(anchor="w", pady=(26, 20))

        ctk.CTkLabel(
            inner,
            text="Local · Offline · SQLite",
            font=font(13),
            text_color="#CFC5B8",
            anchor="w",
        ).pack(anchor="w")

        ctk.CTkLabel(
            inner,
            text="Regular Ramen only",
            font=font(12),
            text_color="#9E9387",
            anchor="w",
        ).pack(anchor="w", pady=(6, 0))

        ctk.CTkLabel(
            brand,
            text="College Capstone Project  ·  Final build",
            font=font(11),
            text_color="#9E9387",
        ).place(relx=0.5, rely=0.94, anchor="center")

    # ------------------------------------------------------------------
    def _build_form_panel(self) -> None:
        area = ctk.CTkFrame(self, fg_color="transparent")
        area.grid(row=0, column=1, sticky="nsew")

        card = ctk.CTkFrame(
            area,
            fg_color=CARD,
            corner_radius=20,
            border_width=1,
            border_color=BORDER,
            width=390,
        )
        card.place(relx=0.5, rely=0.5, anchor="center")

        ctk.CTkLabel(
            card, text="Administrator login", font=font(24, "bold"), text_color=TEXT
        ).pack(anchor="w", padx=32, pady=(34, 2))
        ctk.CTkLabel(
            card,
            text="Sign in to manage your inventory",
            font=font(12),
            text_color=TEXT_MUTED,
        ).pack(anchor="w", padx=32, pady=(0, 26))

        ctk.CTkLabel(
            card, text="Username", font=font(12, "bold"), text_color=TEXT
        ).pack(anchor="w", padx=32)
        self.username_entry = ctk.CTkEntry(
            card,
            placeholder_text="Enter your username",
            height=42,
            corner_radius=10,
            fg_color=PANEL,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT,
            font=font(13),
        )
        self.username_entry.pack(fill="x", padx=32, pady=(6, 18))
        self.username_entry.bind("<Return>", self._handle_login)

        ctk.CTkLabel(
            card, text="Password", font=font(12, "bold"), text_color=TEXT
        ).pack(anchor="w", padx=32)

        password_row = ctk.CTkFrame(card, fg_color="transparent")
        password_row.pack(fill="x", padx=32, pady=(6, 0))
        self.password_entry = ctk.CTkEntry(
            password_row,
            placeholder_text="Enter your password",
            show="•",
            height=42,
            corner_radius=10,
            fg_color=PANEL,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT,
            font=font(13),
        )
        self.password_entry.pack(side="left", fill="x", expand=True)
        self.password_entry.bind("<Return>", self._handle_login)

        self.toggle_button = ctk.CTkButton(
            password_row,
            text="Show",
            command=self._toggle_password,
            width=62,
            height=42,
            corner_radius=10,
            fg_color=PANEL,
            hover_color="#F0E9DD",
            border_width=1,
            border_color=BORDER,
            text_color=TEXT_MUTED,
            font=font(12),
        )
        self.toggle_button.pack(side="left", padx=(8, 0))

        self.error_label = ctk.CTkLabel(
            card,
            text="",
            font=font(12),
            text_color=DANGER,
            wraplength=310,
            justify="left",
            anchor="w",
            height=34,
        )
        self.error_label.pack(fill="x", padx=32, pady=(8, 0))

        self.login_button = ctk.CTkButton(
            card,
            text="Sign in",
            command=self._handle_login,
            height=44,
            corner_radius=10,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            text_color="#FFFFFF",
            font=font(14, "bold"),
        )
        self.login_button.pack(fill="x", padx=32, pady=(4, 10))

        ctk.CTkButton(
            card,
            text="Exit application",
            command=self.on_exit,
            height=36,
            corner_radius=10,
            fg_color="transparent",
            hover_color=PANEL,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT_MUTED,
            font=font(12),
        ).pack(fill="x", padx=32, pady=(0, 14))

        # The hint disappears as soon as the default password is changed.
        if database.default_credentials_active():
            ctk.CTkLabel(
                card,
                text=(
                    "First run  ·  "
                    f"{database.DEFAULT_ADMIN_USERNAME} / "
                    f"{database.DEFAULT_ADMIN_PASSWORD}\n"
                    "You will be asked to set a new password."
                ),
                font=font(11),
                text_color=TEXT_MUTED,
                justify="left",
                anchor="w",
            ).pack(anchor="w", padx=32, pady=(0, 24))

    # ------------------------------------------------------------------
    def _toggle_password(self) -> None:
        self._password_visible = not self._password_visible
        self.password_entry.configure(show="" if self._password_visible else "•")
        self.toggle_button.configure(
            text="Hide" if self._password_visible else "Show"
        )

    def _show_error(self, message: str) -> None:
        self.error_label.configure(text=message)

    def _handle_login(self, event: Any = None) -> None:
        """Validate input, then check credentials against SQLite."""
        if self._busy:
            return
        username = self.username_entry.get().strip()
        password = self.password_entry.get()
        self._show_error("")

        if not username or not password:
            self._show_error("Please enter both username and password.")
            return

        try:
            user = database.verify_credentials(username, password)
        except sqlite3.Error as exc:
            self._show_error(f"Database error: {exc}")
            return

        if user is None:
            self._show_error("Invalid username or password.")
            self.password_entry.delete(0, "end")
            self.password_entry.focus_set()
            return

        self._busy = True
        self.login_button.configure(state="disabled", text="Signing in...")
        self.on_success(user)


class ChangePasswordDialog(ctk.CTkToplevel):
    """Modal password change. on_cancel=None means cancel simply closes."""

    def __init__(
        self,
        master: Any,
        user: dict[str, Any],
        on_success: Callable[[dict[str, Any]], None] | None = None,
        on_cancel: Callable[[], None] | None = None,
        force: bool = False,
    ) -> None:
        super().__init__(master)
        self.user = user
        self.on_success = on_success
        self.on_cancel = on_cancel
        self.force = force

        self.title("Update your password")
        self.resizable(False, False)
        self.configure(fg_color=CREAM)
        self.transient(master)
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
            card, text="Set a new password", font=font(20, "bold"), text_color=TEXT
        ).pack(anchor="w", padx=24, pady=(24, 4))
        message = (
            "For your security, the first login requires a new password."
            if force
            else "Choose a new password for your account."
        )
        ctk.CTkLabel(
            card,
            text=message,
            font=font(12),
            text_color=TEXT_MUTED,
            wraplength=330,
            justify="left",
            anchor="w",
        ).pack(anchor="w", padx=24, pady=(0, 18))

        self.current_entry = self._add_field(card, "Current password", True)
        self.new_entry = self._add_field(card, "New password")
        self.confirm_entry = self._add_field(card, "Confirm new password")

        self.error_label = ctk.CTkLabel(
            card,
            text="",
            font=font(12),
            text_color=DANGER,
            wraplength=330,
            justify="left",
            anchor="w",
            height=34,
        )
        self.error_label.pack(fill="x", padx=24, pady=(10, 0))

        ctk.CTkButton(
            card,
            text="Save password",
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
            text="Cancel and sign out" if force else "Cancel",
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
        width, height = 430, self.winfo_reqheight() + 48
        x = master.winfo_x() + (master.winfo_width() - width) // 2
        y = master.winfo_y() + (master.winfo_height() - height) // 2
        self.geometry(f"{width}x{height}+{max(x, 0)}+{max(y, 0)}")
        self.grab_set()
        self.current_entry.focus_set()

    def _add_field(self, parent: Any, label: str, first: bool = False) -> ctk.CTkEntry:
        ctk.CTkLabel(
            parent, text=label, font=font(12, "bold"), text_color=TEXT
        ).pack(anchor="w", padx=24, pady=(0 if first else 10, 4))
        entry = ctk.CTkEntry(
            parent,
            show="•",
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

    def _save(self) -> None:
        current = self.current_entry.get()
        new_password = self.new_entry.get()
        confirm = self.confirm_entry.get()
        self.error_label.configure(text="")

        try:
            if not current:
                raise ValueError("Please enter your current password.")
            if new_password != confirm:
                raise ValueError("New passwords do not match.")
            database.change_password(self.user["user_id"], current, new_password)
        except (ValueError, sqlite3.Error) as exc:
            self.error_label.configure(text=str(exc))
            return

        updated = dict(self.user)
        updated["must_change_password"] = 0
        self.grab_release()
        self.destroy()
        if self.on_success:
            self.on_success(updated)

    def _cancel(self) -> None:
        self.grab_release()
        self.destroy()
        if self.on_cancel:
            self.on_cancel()
