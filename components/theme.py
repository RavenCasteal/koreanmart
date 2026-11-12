"""
Shared visual tokens for the whole application.

Keeping colours, fonts and spacing in one file makes Stage 2-8 pages look
consistent without copying hex codes around. Warm ramen-inspired palette:
cream background, charcoal text, muted red accents.
"""

import customtkinter as ctk

# Surfaces
CREAM = "#FAF6EF"        # main window background
CARD = "#FFFFFF"         # cards / form surfaces
PANEL = "#FBF7F0"        # input backgrounds
CHARCOAL = "#2B2723"     # sidebar, header, primary text
SIDEBAR_HOVER = "#3D3731"

# Text
TEXT = "#2B2723"
TEXT_MUTED = "#7A7168"
TEXT_ON_DARK = "#FAF6EF"

# Accents
ACCENT = "#B4442E"       # muted ramen red
ACCENT_HOVER = "#8F3522"
BORDER = "#E7DFD3"
SUCCESS = "#3F7D5B"
WARNING = "#C9822B"
DANGER = "#B33A2B"

FONT_FAMILY = "Segoe UI"  # shipped with Windows 10/11


def font(size: int, weight: str = "normal") -> ctk.CTkFont:
    """Build a scaled CustomTkinter font. Call after the root window exists."""
    return ctk.CTkFont(family=FONT_FAMILY, size=size, weight=weight)


def apply_appearance() -> None:
    """The restaurant dashboard is intentionally a LIGHT theme.

    Widgets override the default button/accent colours with our own palette,
    so the built-in colour theme only affects small default details.
    """
    ctk.set_appearance_mode("light")
    ctk.set_default_color_theme("blue")
