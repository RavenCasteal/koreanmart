"""
Stage 4 - shared modal dialog base.

Stages 3-6 all need small forms. Rather than copy the same window setup,
card frame, error label and centring code into every dialog, they subclass
ModalDialog. It is deliberately simple: subclasses still build their own
widgets inside self.card.
"""

from __future__ import annotations

from typing import Any, Callable

import customtkinter as ctk

from components.theme import (
    ACCENT,
    ACCENT_HOVER,
    BORDER,
    CARD,
    CREAM,
    DANGER,
    PANEL,
    TEXT,
    TEXT_MUTED,
    font,
)


class ModalDialog(ctk.CTkToplevel):
    """A centred, modal card window with the application's warm styling."""

    def __init__(self, master: Any, title: str, width: int = 440) -> None:
        super().__init__(master)
        self._width = width
        self.title(title)
        self.resizable(False, False)
        self.configure(fg_color=CREAM)
        self.transient(master.winfo_toplevel())
        # The window close button behaves exactly like Cancel.
        self.protocol("WM_DELETE_WINDOW", self.close)

        self.card = ctk.CTkFrame(
            self,
            fg_color=CARD,
            corner_radius=18,
            border_width=1,
            border_color=BORDER,
        )
        self.card.pack(fill="both", expand=True, padx=24, pady=24)

    # ------------------------------------------------------------------
    # Reusable pieces
    # ------------------------------------------------------------------
    def heading(self, title: str, subtitle: str) -> None:
        ctk.CTkLabel(
            self.card, text=title, font=font(20, "bold"), text_color=TEXT
        ).pack(anchor="w", padx=24, pady=(24, 2))
        ctk.CTkLabel(
            self.card,
            text=subtitle,
            font=font(11),
            text_color=TEXT_MUTED,
            wraplength=self._width - 60,
            justify="left",
            anchor="w",
        ).pack(anchor="w", padx=24, pady=(0, 18))

    def field(
        self, label: str, on_enter: Callable[[], None] | None = None
    ) -> ctk.CTkEntry:
        """A labelled text entry inside the card. Returns the entry."""
        ctk.CTkLabel(
            self.card, text=label, font=font(12, "bold"), text_color=TEXT
        ).pack(anchor="w", padx=24, pady=(12, 4))
        entry = ctk.CTkEntry(
            self.card,
            height=40,
            corner_radius=10,
            fg_color=PANEL,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT,
            font=font(13),
        )
        entry.pack(fill="x", padx=24)
        if on_enter is not None:
            entry.bind("<Return>", lambda _event: on_enter())
        return entry

    def error_label(self, height: int = 32) -> ctk.CTkLabel:
        """Create/remember the red validation message at the bottom."""
        self.error_message = ctk.CTkLabel(
            self.card,
            text="",
            font=font(11),
            text_color=DANGER,
            wraplength=self._width - 60,
            justify="left",
            anchor="w",
            height=height,
        )
        self.error_message.pack(fill="x", padx=24, pady=(8, 0))
        return self.error_message

    def show_error(self, text: str) -> None:
        if hasattr(self, "error_message"):
            self.error_message.configure(text=text)

    def actions(
        self,
        save_text: str,
        on_save: Callable[[], None],
        cancel_text: str | None = "Cancel",
        danger: bool = False,
    ) -> None:
        """Save button + optional Cancel button, both fully wired.

        Pass cancel_text=None for read-only dialogs that need a single
        Close action instead of two identical buttons.
        """
        ctk.CTkButton(
            self.card,
            text=save_text,
            command=on_save,
            height=42,
            corner_radius=10,
            fg_color=DANGER if danger else ACCENT,
            hover_color="#8F2D21" if danger else ACCENT_HOVER,
            text_color="#FFFFFF",
            font=font(13, "bold"),
        ).pack(fill="x", padx=24, pady=(4, 8))
        if cancel_text is not None:
            ctk.CTkButton(
                self.card,
                text=cancel_text,
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

    # ------------------------------------------------------------------
    # Window behaviour
    # ------------------------------------------------------------------
    def finish(self, focus_widget: Any = None) -> None:
        """Call once the subclass has built all of its widgets."""
        self.center()
        self.grab_set()
        if focus_widget is not None:
            focus_widget.focus_set()

    def center(self) -> None:
        self.update_idletasks()
        width = self._width
        height = self.winfo_reqheight() + 48
        root = self.master.winfo_toplevel()
        x = root.winfo_x() + (root.winfo_width() - width) // 2
        y = root.winfo_y() + (root.winfo_height() - height) // 2
        self.geometry(f"{width}x{height}+{max(x, 0)}+{max(y, 0)}")

    def close(self) -> None:
        """Release the modal grab and close (used by Cancel and the X button)."""
        self.grab_release()
        self.destroy()
