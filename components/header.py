"""
Stage 2 - top header bar.

Shows the current page title, today's date and the signed-in administrator.
All three buttons are wired to real behaviour in AppShell / App.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

import customtkinter as ctk

import permissions
from components.theme import (
    ACCENT,
    ACCENT_HOVER,
    BORDER,
    CARD,
    PANEL,
    TEXT,
    TEXT_MUTED,
    font,
)


class Header(ctk.CTkFrame):
    def __init__(
        self,
        master: Any,
        user: dict[str, Any],
        on_refresh: Callable[[], None],
        on_logout: Callable[[], None],
        on_change_password: Callable[[], None],
    ) -> None:
        super().__init__(master, fg_color=CARD, corner_radius=0, height=78)
        self.pack_propagate(False)

        # 1px bottom separator.
        ctk.CTkFrame(self, fg_color=BORDER, height=1).pack(side="bottom", fill="x")

        left = ctk.CTkFrame(self, fg_color="transparent")
        left.pack(side="left", padx=26, pady=12)
        self.title_label = ctk.CTkLabel(
            left, text="Dashboard", font=font(20, "bold"), text_color=TEXT
        )
        self.title_label.pack(anchor="w")
        # Stage 13: a long page subtitle used to squeeze the right-hand pack
        # group, clipping the date to "ov 12, 202". Wrapping the subtitle at
        # 290px keeps the left group narrow enough for every button, the user
        # label AND the full date to get their requested widths.
        self.subtitle_label = ctk.CTkLabel(
            left, text="", font=font(11), text_color=TEXT_MUTED,
            wraplength=290, justify="left", anchor="w",
        )
        self.subtitle_label.pack(anchor="w")

        right = ctk.CTkFrame(self, fg_color="transparent")
        right.pack(side="right", padx=16)

        ctk.CTkButton(
            right,
            text="Log out",
            command=on_logout,
            width=88,
            height=34,
            corner_radius=8,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            text_color="#FFFFFF",
            font=font(12, "bold"),
        ).pack(side="right", padx=(6, 0))

        ctk.CTkButton(
            right,
            text="Change password",
            command=on_change_password,
            width=112,
            height=34,
            corner_radius=8,
            fg_color=PANEL,
            hover_color="#F0E9DD",
            border_width=1,
            border_color=BORDER,
            text_color=TEXT_MUTED,
            font=font(11),
        ).pack(side="right", padx=6)

        ctk.CTkButton(
            right,
            text="Refresh",
            command=on_refresh,
            width=66,
            height=34,
            corner_radius=8,
            fg_color=PANEL,
            hover_color="#F0E9DD",
            border_width=1,
            border_color=BORDER,
            text_color=TEXT_MUTED,
            font=font(11),
        ).pack(side="right", padx=6)

        self.user_label = ctk.CTkLabel(
            right,
            text=self._user_text(user),
            font=font(12, "bold"),
            text_color=TEXT,
        )
        self.user_label.pack(side="right", padx=(0, 8))

        self.date_label = ctk.CTkLabel(
            right,
            text=datetime.now().strftime("%b %d, %Y"),
            font=font(11),
            text_color=TEXT_MUTED,
            width=76,
            anchor="e",
        )
        self.date_label.pack(side="right", padx=(0, 6))

    def set_title(self, title: str, subtitle: str) -> None:
        self.title_label.configure(text=title)
        self.subtitle_label.configure(text=subtitle)

    @staticmethod
    def _user_text(user: dict[str, Any]) -> str:
        """Show the signed-in role so the counter PC is never ambiguous."""
        name = user.get("username", "admin")
        return str(name) + "  ·  " + permissions.role_label(user)

    def update_user(self, user: dict[str, Any]) -> None:
        self.user_label.configure(text=self._user_text(user))
