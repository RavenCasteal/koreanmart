"""
Neighborhood Mart - application entry point (Stage 12 build)
=======================================================================
Run with:  .venv\\Scripts\\python.exe main.py
       or: double-click run.bat          (no console window)
       or: double-click run_debug.bat    (console visible, for debugging)

Flow: initialize SQLite -> Stage 12 product/barcode bootstrap -> login ->
      (first-run password change) -> main shell (sidebar + header + 11 pages,
      including the barcode Point of Sale) -> logout back to login.

Stage 10: the signed-in role filters the sidebar. A manager sees all 11
pages; a cashier sees only Point of Sale and lands there directly.
Stage 11a adds the manager-only Menu page and per-dish recipes (retained as
legacy tools). Stage 12 adds the Products / barcode catalog and replaces the
live POS with a scan-to-ring-up basket; all money stays Philippine peso.

The App class only swaps between two top-level views (LoginView and
AppShell). Page navigation inside the shell is handled by components/shell.py.

Because run.bat launches pythonw.exe there is no console, so every uncaught
error is also written to logs/error.log with a full traceback.
"""

from __future__ import annotations

import sqlite3
import sys
import traceback
from datetime import datetime
from pathlib import Path
from tkinter import messagebox
from typing import Any

import customtkinter as ctk

import database
import permissions
import products
from components.shell import AppShell
from components.theme import CREAM, apply_appearance
from pages.login import ChangePasswordDialog, LoginView

BASE_DIR = Path(__file__).resolve().parent
ICON_PATH = BASE_DIR / "assets" / "images" / "ramen_icon.ico"
ERROR_LOG = BASE_DIR / "logs" / "error.log"

WINDOW_WIDTH = 1160
WINDOW_HEIGHT = 720


# ----------------------------------------------------------------------
# Error reporting (Stage 8)
# ----------------------------------------------------------------------
def log_exception(title: str, exc: BaseException) -> None:
    """Append a full traceback to logs/error.log.

    Logging is deliberately best-effort: a failure to write the log must
    never hide or replace the error the user actually needs to see.
    """
    try:
        ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with ERROR_LOG.open("a", encoding="utf-8") as handle:
            handle.write(f"\n{'=' * 70}\n{stamp}  {title}\n")
            traceback.print_exception(
                type(exc), exc, exc.__traceback__, file=handle
            )
    except (OSError, ValueError):
        pass


def show_error_dialog(title: str, message: str) -> None:
    """Show a GUI message box; ignore failures when no display is available."""
    try:
        messagebox.showerror(title, message)
    except Exception:
        pass


def _global_excepthook(exc_type: Any, exc_value: Any, exc_tb: Any) -> None:
    """Catch errors outside Tk callbacks (pythonw has no stderr console)."""
    log_exception("Unhandled error", exc_value)
    if sys.stderr is not None:
        try:
            sys.__excepthook__(exc_type, exc_value, exc_tb)
        except Exception:
            pass


class App(ctk.CTk):
    """One root window; screens are CTkFrames swapped inside it."""

    def __init__(self) -> None:
        super().__init__()
        self.current_user: dict[str, Any] | None = None
        self.view: ctk.CTkFrame | None = None

        self.title("Neighborhood Mart  |  Inventory & Point of Sale")
        self.minsize(1000, 640)
        self.configure(fg_color=CREAM)
        self._set_icon()
        self._center_window()
        self.show_login()

    def _set_icon(self) -> None:
        """iconbitmap() is Windows-only and optional, so keep it guarded."""
        if not sys.platform.startswith("win") or not ICON_PATH.exists():
            return
        try:
            self.iconbitmap(str(ICON_PATH))
        except Exception:
            pass

    def report_callback_exception(self, exc: Any, val: Any, tb: Any) -> None:
        """Tkinter routes uncaught widget-callback exceptions here."""
        log_exception("Unhandled GUI error", val)
        show_error_dialog(
            "Unexpected error",
            "Something went wrong while using the application.\n\n"
            f"Details: {val}\n\n"
            f"A full traceback was saved to:\n{ERROR_LOG}",
        )

    def _center_window(self) -> None:
        self.update_idletasks()
        x = max((self.winfo_screenwidth() - WINDOW_WIDTH) // 2, 0)
        y = max((self.winfo_screenheight() - WINDOW_HEIGHT) // 2, 0)
        self.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}+{x}+{y}")

    def _swap(self, view_class: Any, **kwargs: Any) -> None:
        """Destroy the current screen and show another one."""
        if self.view is not None:
            self.view.destroy()
        self.view = view_class(self, **kwargs)
        self.view.pack(fill="both", expand=True)

    # ------------------------------------------------------------------
    def show_login(self) -> None:
        permissions.clear_session()
        self.current_user = None
        self._swap(
            LoginView,
            on_success=self._on_login_success,
            on_exit=self.destroy,
        )

    def _on_login_success(self, user: dict[str, Any]) -> None:
        """Force a password change while the default credentials are active."""
        if user.get("must_change_password"):
            dialog = ChangePasswordDialog(
                self,
                user,
                on_success=self.show_shell,
                on_cancel=self.show_login,
                force=True,
            )
            self.wait_window(dialog)
        else:
            self.show_shell(user)

    def show_shell(self, user: dict[str, Any]) -> None:
        self.current_user = user
        self._swap(
            AppShell,
            user=user,
            on_logout=self.show_login,
            on_change_password=self._open_change_password,
            on_exit=self.destroy,
        )

    def _open_change_password(self) -> None:
        if self.current_user is None:
            return
        ChangePasswordDialog(
            self,
            self.current_user,
            on_success=self._apply_password_change,
            on_cancel=None,
            force=False,
        )

    def _apply_password_change(self, user: dict[str, Any]) -> None:
        """Update the session without resetting the current page."""
        self.current_user = user
        permissions.set_session(user)
        if isinstance(self.view, AppShell):
            self.view.update_user(user)


def main() -> int:
    """Initialize the database, then start the GUI event loop."""
    sys.excepthook = _global_excepthook
    try:
        database.initialize_database()
        # Stage 12: additive product/barcode schema + one-time catalog reset.
        # Both calls are idempotent; user accounts and password hashes are
        # never touched. database.py itself stays on its v1-v4 chain.
        products.ensure_schema()
        products.bootstrap_catalog()
    except (sqlite3.Error, OSError) as exc:
        log_exception("Database initialization failed", exc)
        show_error_dialog(
            "Database initialization failed",
            "The local SQLite database could not be created.\n\n"
            f"Details: {exc}\n\n"
            f"A full traceback was saved to:\n{ERROR_LOG}",
        )
        return 1

    try:
        apply_appearance()
        app = App()
        app.mainloop()
    except Exception as exc:
        log_exception("Startup failed", exc)
        show_error_dialog(
            "Application failed to start",
            f"Details: {exc}\n\n"
            f"A full traceback was saved to:\n{ERROR_LOG}",
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
