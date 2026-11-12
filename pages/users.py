"""
Stage 10 - User accounts screen (MANAGER ONLY).

Only the manager reaches this page: AppShell filters the sidebar by role and
navigate() refuses forbidden keys, and users_repository.require() checks the
capability again before any write.

What a manager can do here:
  * see every account (username, name, role, active flag, last login)
  * add a Manager or Cashier with a temporary password
  * deactivate / reactivate an account (never deleted - history keeps its FK)
  * reset a temporary password

New accounts are forced to change their password on first login. Cashier
accounts see ONLY the Point of Sale screen; they never see this page.
"""

from __future__ import annotations

import sqlite3
from tkinter import messagebox
from typing import Any, Callable

import customtkinter as ctk

import permissions
import users_repository as users
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

TABLE_WEIGHTS = (2, 3, 2, 2, 3, 4)
TABLE_MINSIZES = (120, 160, 100, 95, 150, 210)
TABLE_TITLES = ("Username", "Full name", "Role", "Status", "Last login", "Actions")


class UserDialog(ModalDialog):
    """Create one account with a temporary password."""

    def __init__(self, master: Any, actor: Any, on_saved: Callable[[], None]):
        super().__init__(master, "Add user", 460)
        self.actor = actor
        self.on_saved = on_saved
        self.heading(
            "Add a user account",
            "Cashiers see only the Point of Sale screen. The temporary "
            "password must be changed at the user's first login.",
        )
        self.username = self.field("Username")
        self.full_name = self.field("Full name")
        ctk.CTkLabel(
            self.card, text="Role", font=font(12, "bold"), text_color=TEXT
        ).pack(anchor="w", padx=24, pady=(12, 4))
        self.role = ctk.CTkComboBox(
            self.card,
            values=["Manager", "Cashier"],
            height=40,
            corner_radius=10,
            fg_color=PANEL,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT,
            font=font(13),
        )
        self.role.pack(fill="x", padx=24)
        self.role.set("Cashier")
        self.password = self.field("Temporary password")
        self.password.configure(show="*")
        self.confirm = self.field("Confirm temporary password")
        self.confirm.configure(show="*")
        self.error_label()
        self.actions("Create account", self._save)
        self.finish(self.username)

    def _save(self) -> None:
        self.show_error("")
        password = self.password.get()
        if password != self.confirm.get():
            self.show_error("The temporary passwords do not match.")
            return
        try:
            users.create_user(
                self.username.get(),
                password,
                self.full_name.get(),
                self.role.get().strip().lower(),
                actor=self.actor,
            )
        except (ValueError, sqlite3.Error, permissions.PermissionDenied) as exc:
            self.show_error(str(exc))
            return
        self.close()
        self.on_saved()


class ResetPasswordDialog(ModalDialog):
    """Manager sets a new short-lived temporary password."""

    def __init__(
        self,
        master: Any,
        actor: Any,
        target: dict[str, Any],
        on_saved: Callable[[], None],
    ):
        super().__init__(master, "Reset password", 440)
        self.actor = actor
        self.target = target
        self.on_saved = on_saved
        self.heading(
            "Reset temporary password",
            "Account: " + str(target["username"]) + ". The user will be "
            "asked to choose their own password at the next login.",
        )
        self.password = self.field("New temporary password")
        self.password.configure(show="*")
        self.confirm = self.field("Confirm temporary password")
        self.confirm.configure(show="*")
        self.error_label()
        self.actions("Reset password", self._save)
        self.finish(self.password)

    def _save(self) -> None:
        self.show_error("")
        password = self.password.get()
        if password != self.confirm.get():
            self.show_error("The temporary passwords do not match.")
            return
        try:
            users.reset_password(
                self.target["user_id"], password, actor=self.actor
            )
        except (ValueError, sqlite3.Error, permissions.PermissionDenied) as exc:
            self.show_error(str(exc))
            return
        self.close()
        self.on_saved()


class UsersView(ctk.CTkFrame):
    """Manager-only account list with add / deactivate / reset actions."""

    def __init__(
        self,
        master: Any,
        user: dict[str, Any] | None = None,
        on_navigate: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(master, fg_color=CREAM, corner_radius=0)
        self.actor = user or {}
        self.on_navigate = on_navigate
        # Third line of defence: even if a caller manages to instantiate this
        # view directly, a cashier sees no account data and no action buttons.
        self.allowed = permissions.can(
            self.actor, permissions.CAP_MANAGE_USERS
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

    def _build_denied(self) -> None:
        """Read-only fallback shown if a non-manager ever reaches this view."""
        ctk.CTkLabel(
            self.container,
            text="Manager access required",
            font=font(20, "bold"),
            text_color=TEXT,
            anchor="w",
        ).pack(anchor="w", pady=(8, 6))
        ctk.CTkLabel(
            self.container,
            text="User accounts are visible only to a manager. "
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
            left, text="User accounts", font=font(20, "bold"), text_color=TEXT
        ).pack(anchor="w")
        ctk.CTkLabel(
            left,
            text="Manager only  ·  cashiers see just the Point of Sale screen",
            font=font(11),
            text_color=TEXT_MUTED,
            anchor="w",
        ).pack(anchor="w")
        ctk.CTkButton(
            top,
            text="Add user",
            command=self._add_user,
            width=130,
            height=34,
            corner_radius=8,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            text_color="#FFFFFF",
            font=font(12, "bold"),
        ).pack(side="right")
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
        self.status_label.pack(fill="x", pady=(0, 10))

    def _build_table(self) -> None:
        panel = ctk.CTkFrame(
            self.container,
            fg_color=CARD,
            corner_radius=14,
            border_width=1,
            border_color=BORDER,
        )
        panel.pack(fill="x")
        header = self._grid_row(panel, TABLE_WEIGHTS, TABLE_MINSIZES, PANEL)
        header.pack(fill="x", padx=16, pady=(16, 6))
        for column, title in enumerate(TABLE_TITLES):
            self._cell(header, column, title, header=True)
        self.rows_frame = ctk.CTkFrame(panel, fg_color="transparent")
        self.rows_frame.pack(fill="x", padx=16, pady=(0, 8))
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
            "Passwords are stored only as PBKDF2-HMAC-SHA256 hashes. "
            "The temporary password is never stored in plain text.",
            "New and reset accounts must set their own password at first login.",
            "Deactivating is a status change, never a delete - sales and "
            "stock history keep their creator references.",
            "The last active manager cannot be deactivated, and you cannot "
            "deactivate your own account.",
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

    def _message(self, text: str, *, error: bool = False) -> None:
        self.status_label.configure(
            text=text, text_color=DANGER if error else SUCCESS
        )

    def refresh(self) -> None:
        if not self.allowed:
            return
        try:
            rows = users.list_users(actor=self.actor)
            counts = users.summary(actor=self.actor)
        except (sqlite3.Error, ValueError) as exc:
            self._message(str(exc), error=True)
            return
        self._render_summary(counts)
        self._render_rows(rows)

    def _render_summary(self, counts: dict[str, int]) -> None:
        for child in self.summary_frame.winfo_children():
            child.destroy()
        cards = (
            ("Total accounts", str(counts["total"]), "All roles", TEXT_MUTED),
            ("Active", str(counts["active"]), "Can sign in", SUCCESS),
            ("Managers", str(counts["managers"]), "Full access", ACCENT),
            ("Cashiers", str(counts["cashiers"]), "POS only", WARNING),
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
                card, text=value, font=font(21, "bold"), text_color=TEXT, anchor="w"
            ).pack(fill="x", padx=18, pady=(2, 0))
            ctk.CTkLabel(
                card, text=note, font=font(11), text_color=TEXT_MUTED, anchor="w"
            ).pack(fill="x", padx=18, pady=(2, 16))

    def _render_rows(self, rows: list[dict[str, Any]]) -> None:
        for child in self.rows_frame.winfo_children():
            child.destroy()
        for item in rows:
            row = self._grid_row(
                self.rows_frame, TABLE_WEIGHTS, TABLE_MINSIZES, CARD
            )
            row.pack(fill="x", pady=2)
            active = bool(item["is_active"])
            self._cell(row, 0, str(item["username"]))
            self._cell(row, 1, str(item["full_name"]))
            self._cell(row, 2, permissions.role_label(item["role"]))
            self._cell(
                row, 3, "Active" if active else "Inactive",
                color=SUCCESS if active else DANGER,
            )
            last = item["last_login_at"] or "Never"
            self._cell(row, 4, str(last))
            actions = ctk.CTkFrame(row, fg_color="transparent")
            actions.grid(row=0, column=5, sticky="w", padx=4, pady=5)
            ctk.CTkButton(
                actions,
                text="Reset password",
                command=lambda target=item: self._reset_password(target),
                width=110,
                height=24,
                corner_radius=6,
                fg_color=PANEL,
                hover_color="#F0E9DD",
                border_width=1,
                border_color=BORDER,
                text_color=TEXT_MUTED,
                font=font(10),
            ).pack(side="left", padx=(0, 4))
            ctk.CTkButton(
                actions,
                text="Deactivate" if active else "Reactivate",
                command=lambda target=item, flag=active: self._toggle(target, flag),
                width=88,
                height=24,
                corner_radius=6,
                fg_color=DANGER if active else SUCCESS,
                hover_color="#8F2D21" if active else "#326349",
                text_color="#FFFFFF",
                font=font(10),
            ).pack(side="left")
        self.table_note.configure(
            text=(
                "Showing " + str(len(rows)) + " account(s). "
                "Accounts with a pending password change must set a new "
                "password the next time they sign in."
            )
        )

    def _add_user(self) -> None:
        UserDialog(self, self.actor, self._after_change)

    def _reset_password(self, target: dict[str, Any]) -> None:
        ResetPasswordDialog(self, self.actor, target, self._after_change)

    def _toggle(self, target: dict[str, Any], currently_active: bool) -> None:
        if currently_active:
            answer = messagebox.askyesno(
                "Deactivate account",
                "Deactivate " + str(target["username"]) + "?\n\n"
                "They will no longer be able to sign in. All past sales and "
                "stock records are kept.",
            )
            if not answer:
                return
        try:
            users.set_user_active(
                target["user_id"], not currently_active, actor=self.actor
            )
        except (ValueError, sqlite3.Error, permissions.PermissionDenied) as exc:
            self._message(str(exc), error=True)
            return
        self._message(
            "Account " + str(target["username"]) + " "
            + ("deactivated." if currently_active else "reactivated.")
        )
        self.refresh()

    def _after_change(self) -> None:
        self._message("Account saved. The temporary password must be "
                      "changed at the user's first login.")
        self.refresh()

    @staticmethod
    def _grid_row(
        parent: Any,
        weights: tuple[int, ...],
        minsizes: tuple[int, ...],
        background: str,
    ) -> ctk.CTkFrame:
        row = ctk.CTkFrame(parent, fg_color=background, corner_radius=8)
        for index, weight in enumerate(weights):
            row.grid_columnconfigure(
                index,
                weight=weight,
                minsize=minsizes[index],
                uniform="users_table",
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
            wraplength=170,
            justify="left",
        ).grid(row=0, column=column, sticky="w", padx=6, pady=7)
