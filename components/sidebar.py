"""
Stage 2 - navigation sidebar.

The sidebar only reports which page was clicked; the AppShell decides what
to display. The active item is highlighted by AppShell via set_active().
"""

from __future__ import annotations

from typing import Any, Callable

import customtkinter as ctk

from components.theme import (
    ACCENT,
    ACCENT_HOVER,
    CHARCOAL,
    SIDEBAR_HOVER,
    TEXT_ON_DARK,
    font,
)

DARK_MUTED = "#9E9387"
DARK_SOFT = "#CFC5B8"


class Sidebar(ctk.CTkFrame):
    """Dark charcoal navigation column."""

    def __init__(
        self,
        master: Any,
        items: list[tuple[str, str]],
        on_navigate: Callable[[str], None],
        menu_item: str,
        on_exit: Callable[[], None],
    ) -> None:
        super().__init__(master, fg_color=CHARCOAL, corner_radius=0, width=240)
        self.on_navigate = on_navigate
        self._buttons: dict[str, ctk.CTkButton] = {}

        brand = ctk.CTkFrame(self, fg_color="transparent")
        brand.pack(fill="x", padx=22, pady=(20, 0))

        ctk.CTkLabel(
            brand,
            text="MART",
            fg_color=ACCENT,
            corner_radius=6,
            text_color="#FFFFFF",
            font=font(10, "bold"),
            width=64,
            height=22,
        ).pack(anchor="w")

        ctk.CTkLabel(
            brand,
            text="Inventory\n& POS",
            font=font(23, "bold"),
            text_color=TEXT_ON_DARK,
            justify="left",
            anchor="w",
        ).pack(anchor="w", pady=(10, 0))

        ctk.CTkFrame(
            brand, fg_color=ACCENT, width=44, height=3, corner_radius=2
        ).pack(anchor="w", pady=(12, 12))

        ctk.CTkLabel(
            brand,
            text="STORE",
            font=font(10, "bold"),
            text_color=DARK_MUTED,
        ).pack(anchor="w")
        ctk.CTkLabel(
            brand,
            text=menu_item,
            font=font(13, "bold"),
            text_color=DARK_SOFT,
            wraplength=190,
            justify="left",
            anchor="w",
        ).pack(anchor="w", pady=(2, 0))

        ctk.CTkLabel(
            self,
            text="NAVIGATION",
            font=font(10, "bold"),
            text_color=DARK_MUTED,
        ).pack(anchor="w", padx=24, pady=(12, 6))

        # Stage 11a: a tenth page made a plain packed list taller than the
        # 720px window, clipping the last navigation button and the footer.
        # The scrollable area takes all remaining height, so the brand stays
        # pinned at the top, every page stays reachable by scrolling, and the
        # exit footer stays visible at the bottom even at minsize(1000, 640).
        self.nav_frame = ctk.CTkScrollableFrame(
            self,
            fg_color=CHARCOAL,
            corner_radius=0,
            border_width=0,
            scrollbar_fg_color=CHARCOAL,
            scrollbar_button_color="#5A5149",
            scrollbar_button_hover_color=SIDEBAR_HOVER,
        )
        self.nav_frame.pack(fill="both", expand=True, pady=(0, 4))

        for key, label in items:
            button = ctk.CTkButton(
                self.nav_frame,
                text=f"  {label}",
                anchor="w",
                height=32,
                corner_radius=9,
                fg_color="transparent",
                hover_color=SIDEBAR_HOVER,
                text_color=DARK_SOFT,
                font=font(13),
                command=lambda selected=key: self.on_navigate(selected),
            )
            button.pack(fill="x", padx=14, pady=1)
            self._buttons[key] = button

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(fill="x", padx=18, pady=(0, 20))

        self.exit_button = ctk.CTkButton(
            footer,
            text="Exit application",
            command=on_exit,
            height=34,
            corner_radius=8,
            fg_color="transparent",
            hover_color=SIDEBAR_HOVER,
            border_width=1,
            border_color="#5A5149",
            text_color=DARK_MUTED,
            font=font(11),
        )
        self.exit_button.pack(fill="x")

        ctk.CTkLabel(
            footer,
            text="Local · Offline · SQLite",
            font=font(10),
            text_color=DARK_MUTED,
        ).pack(anchor="center", pady=(10, 0))

    def set_active(self, key: str) -> None:
        """Highlight one navigation button and reset the others."""
        for item_key, button in self._buttons.items():
            if item_key == key:
                button.configure(
                    fg_color=ACCENT,
                    hover_color=ACCENT_HOVER,
                    text_color="#FFFFFF",
                    font=font(13, "bold"),
                )
            else:
                button.configure(
                    fg_color="transparent",
                    hover_color=SIDEBAR_HOVER,
                    text_color=DARK_SOFT,
                    font=font(13),
                )
