"""
Stage 18 - manager-only database tools dialog.

This is deliberately a DIALOG on the existing Inventory screen, not a 13th
sidebar page: the shell's 12-page registry and the role-based page list are
pinned by the Stage 9-13 regression suite, and this stage is a database
change, not a navigation redesign.

It exposes only what the new data needs:
  * add / list suppliers (optional on a stock-in header)
  * read the received-batch ledger in FEFO order
  * run the batch-vs-on-hand reconciliation check (read-only, never repairs)
  * create a WAL-safe SQLite backup, and restore a verified one

Restore is gated behind an explicit tick box because it overwrites the live
database. A pre-restore safety backup is always taken by backups.py.

All SQL lives in batches.py / backups.py. This file only builds the UI.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Callable

import customtkinter as ctk

import backups
import batches
import permissions
from components.dialog import ModalDialog
from components.theme import (
    ACCENT,
    ACCENT_HOVER,
    BORDER,
    CARD,
    CREAM,
    PANEL,
    TEXT,
    TEXT_MUTED,
    font,
)
from formatting import format_money, format_number

# A short preview; the full ledger is available through batches.list_batches().
# Keeping the list compact lets the backup controls stay on screen.
MAX_BATCH_ROWS = 3


def _section(parent: Any, title: str, subtitle: str) -> Any:
    """A titled card body. CTkFrame defaults to height=200, so force 1."""
    frame = ctk.CTkFrame(
        parent, fg_color=PANEL, corner_radius=10,
        border_width=1, border_color=BORDER, height=1,
    )
    frame.pack(fill="x", padx=24, pady=(0, 12))
    ctk.CTkLabel(
        frame, text=title, font=font(13, "bold"), text_color=TEXT,
    ).pack(anchor="w", padx=14, pady=(10, 0))
    ctk.CTkLabel(
        frame, text=subtitle, font=font(10), text_color=TEXT_MUTED,
        wraplength=660, justify="left", anchor="w",
    ).pack(anchor="w", padx=14, pady=(0, 8))
    return frame


class DatabaseToolsDialog(ModalDialog):
    """Batch / supplier / backup controls, manager only."""

    WIDTH = 760
    HEIGHT = 900

    def __init__(
        self,
        master: Any,
        on_done: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(master, "Database tools", self.WIDTH)
        self.on_done = on_done
        if not permissions.can(
            permissions.current_session(), permissions.CAP_MANAGE_BATCHES
        ):
            self._denied = True
        else:
            self._denied = False

        # Fixed footer + scrollable body, matching TransactionDialog so the
        # status line and Close button stay visible on a short screen.
        self.card.destroy()
        self.outer_card = ctk.CTkFrame(
            self, fg_color=CARD, corner_radius=18,
            border_width=1, border_color=BORDER,
        )
        self.outer_card.pack(fill="both", expand=True, padx=20, pady=20)
        self.footer = ctk.CTkFrame(self.outer_card, fg_color="transparent")
        self.footer.pack(side="bottom", fill="x")
        self.scroller = ctk.CTkScrollableFrame(
            self.outer_card, fg_color=CARD, corner_radius=0
        )
        self.scroller.pack(side="top", fill="both", expand=True, padx=20)
        self.card = ctk.CTkFrame(self.scroller, fg_color="transparent", height=1)
        self.card.pack(fill="both", expand=True, pady=(18, 4))

        self.heading(
            "Database tools",
            "Batches, suppliers, balance reconciliation and WAL-safe SQLite "
            "backups. Manager only.",
        )
        if self._denied:
            ctk.CTkLabel(
                self.card,
                text="Your account is not allowed to manage the database.",
                font=font(12), text_color=TEXT,
            ).pack(anchor="w", padx=24, pady=12)
        else:
            self._build_suppliers()
            self._build_batches()
            self._build_backups()

        self.status = ctk.CTkLabel(
            self.footer, text="", font=font(11), text_color=TEXT_MUTED,
            wraplength=self.WIDTH - 96, justify="left", anchor="w", height=34,
        )
        self.status.pack(fill="x", padx=24, pady=(4, 0))
        ctk.CTkButton(
            self.footer, text="Close", command=self.close, height=40,
            corner_radius=10, fg_color=ACCENT, hover_color=ACCENT_HOVER,
            text_color="#FFFFFF", font=font(13, "bold"),
        ).pack(fill="x", padx=24, pady=(2, 16))
        self.finish()

    # ------------------------------------------------------------------
    # Feedback
    # ------------------------------------------------------------------
    def _set_status(self, text: str, ok: bool = True) -> None:
        self.status.configure(text=text, text_color=TEXT if ok else "#B23A2C")

    def center(self) -> None:
        self.update_idletasks()
        root = self.master.winfo_toplevel()
        height = min(self.HEIGHT, max(360, root.winfo_screenheight() - 100))
        width = self.WIDTH
        x = root.winfo_x() + (root.winfo_width() - width) // 2
        y = root.winfo_y() + (root.winfo_height() - height) // 2
        self.geometry(f"{width}x{height}+{max(x, 0)}+{max(y, 0)}")

    # ------------------------------------------------------------------
    # Suppliers
    # ------------------------------------------------------------------
    def _build_suppliers(self) -> None:
        box = _section(
            self.card, "Suppliers",
            "Optional on a stock-in delivery. A supplier is stored on the "
            "received batch, so later edits cannot rewrite history.",
        )
        row = ctk.CTkFrame(box, fg_color="transparent", height=1)
        row.pack(fill="x", padx=14, pady=(0, 6))
        self.supplier_name = ctk.CTkEntry(
            row, placeholder_text="Supplier name", height=34,
            fg_color=CARD, border_width=1, border_color=BORDER,
            text_color=TEXT, font=font(11),
        )
        self.supplier_name.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.supplier_contact = ctk.CTkEntry(
            row, placeholder_text="Contact (optional)", width=180, height=34,
            fg_color=CARD, border_width=1, border_color=BORDER,
            text_color=TEXT, font=font(11),
        )
        self.supplier_contact.pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            row, text="Add", width=76, height=34, command=self._add_supplier,
            corner_radius=8, fg_color=ACCENT, hover_color=ACCENT_HOVER,
            text_color="#FFFFFF", font=font(11, "bold"),
        ).pack(side="left")
        self.supplier_list = ctk.CTkLabel(
            box, text="", font=font(10), text_color=TEXT_MUTED,
            wraplength=660, justify="left", anchor="w",
        )
        self.supplier_list.pack(anchor="w", padx=14, pady=(0, 10))
        self._refresh_suppliers()

    def _refresh_suppliers(self) -> None:
        try:
            rows = batches.list_suppliers()
        except (ValueError, sqlite3.Error) as exc:
            self.supplier_list.configure(text=f"Could not load suppliers: {exc}")
            return
        if not rows:
            self.supplier_list.configure(text="No suppliers yet.")
            return
        self.supplier_list.configure(
            text="  |  ".join(
                f"{row['name']} (#{row['supplier_id']})" for row in rows
            )
        )

    def _add_supplier(self) -> None:
        try:
            supplier_id = batches.add_supplier(
                self.supplier_name.get(), self.supplier_contact.get()
            )
        except (ValueError, sqlite3.Error) as exc:
            self._set_status(str(exc), ok=False)
            return
        self.supplier_name.delete(0, "end")
        self.supplier_contact.delete(0, "end")
        self._refresh_suppliers()
        self._set_status(f"Supplier #{supplier_id} added.")

    # ------------------------------------------------------------------
    # Batch ledger + reconciliation
    # ------------------------------------------------------------------
    def _build_batches(self) -> None:
        box = _section(
            self.card, "Received batches (FEFO order)",
            "Earliest expiry first; batches with no expiry are used last, "
            "oldest receipt first. Unknown expiry / cost stay blank.",
        )
        self.batch_body = ctk.CTkFrame(box, fg_color="transparent", height=1)
        self.batch_body.pack(fill="x", padx=14, pady=(0, 6))
        ctk.CTkButton(
            box, text="Check balance reconciliation", height=32,
            command=self._reconcile, corner_radius=8,
            fg_color=PANEL, hover_color="#F0E9DD",
            border_width=1, border_color=BORDER,
            text_color=TEXT, font=font(11, "bold"),
        ).pack(fill="x", padx=14, pady=(0, 10))
        self._refresh_batches()

    def _refresh_batches(self) -> None:
        for child in self.batch_body.winfo_children():
            child.destroy()
        try:
            rows = batches.list_batches()
        except (ValueError, sqlite3.Error) as exc:
            ctk.CTkLabel(
                self.batch_body, text=f"Could not load batches: {exc}",
                font=font(10), text_color=TEXT_MUTED, anchor="w",
            ).pack(anchor="w")
            return
        if not rows:
            ctk.CTkLabel(
                self.batch_body, text="No batches with stock on hand.",
                font=font(10), text_color=TEXT_MUTED, anchor="w",
            ).pack(anchor="w")
            return
        for row in rows[:MAX_BATCH_ROWS]:
            expiry = row["expiration_date"] or "no expiry"
            cost = (
                "cost unknown" if row["unit_cost"] is None
                else format_money(float(row["unit_cost"])) + "/unit"
            )
            supplier = row["supplier_name"]
            opening = "  [opening]" if row["is_opening"] else ""
            text = (
                f"#{row['batch_id']}  {row['product_name']}{opening}  |  "
                f"{format_number(row['remaining_qty'])} / "
                f"{format_number(row['received_qty'])} {row['unit']} left  |  "
                f"{expiry}  |  {cost}  |  {supplier}"
            )
            ctk.CTkLabel(
                self.batch_body, text=text, font=font(10), text_color=TEXT,
                anchor="w", wraplength=660, justify="left",
            ).pack(anchor="w", pady=1)
        if len(rows) > MAX_BATCH_ROWS:
            ctk.CTkLabel(
                self.batch_body,
                text=f"... and {len(rows) - MAX_BATCH_ROWS} more batch(es).",
                font=font(10), text_color=TEXT_MUTED, anchor="w",
            ).pack(anchor="w")

    def _reconcile(self) -> None:
        try:
            report = batches.reconcile()
        except (ValueError, sqlite3.Error) as exc:
            self._set_status(str(exc), ok=False)
            return
        if report["ok"]:
            self._set_status(
                f"Reconciliation OK - {report['products_checked']} products, "
                f"{report['batches_checked']} batches, no mismatches."
            )
        else:
            self._set_status(
                f"MISMATCH: {len(report['product_mismatches'])} product(s), "
                f"{len(report['batch_mismatches'])} batch(es) disagree. "
                "Nothing was changed; review the inventory data.",
                ok=False,
            )

    # ------------------------------------------------------------------
    # Backup / restore
    # ------------------------------------------------------------------
    def _build_backups(self) -> None:
        box = _section(
            self.card, "SQLite backup / restore",
            "Backups use SQLite's online backup API, so committed WAL frames "
            "are included. Restoring replaces live data - close and relaunch "
            "the app afterwards. A pre-restore safety backup is automatic.",
        )
        ctk.CTkButton(
            box, text="Create backup now", height=32,
            command=self._create_backup, corner_radius=8,
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
            text_color="#FFFFFF", font=font(11, "bold"),
        ).pack(fill="x", padx=14, pady=(0, 8))
        self.backup_combo = ctk.CTkComboBox(
            box, values=[], height=32, corner_radius=8,
            fg_color=CARD, border_width=1, border_color=BORDER,
            text_color=TEXT, font=font(10), dropdown_font=font(10),
        )
        self.backup_combo.pack(fill="x", padx=14)
        self.confirm_restore = ctk.CTkCheckBox(
            box,
            text="I understand restoring replaces the current database.",
            font=font(10), text_color=TEXT_MUTED,
            checkmark_color="#FFFFFF", fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
        )
        self.confirm_restore.pack(anchor="w", padx=14, pady=(6, 2))
        ctk.CTkButton(
            box, text="Restore selected backup", height=32,
            command=self._restore_backup, corner_radius=8,
            fg_color=PANEL, hover_color="#F0E9DD",
            border_width=1, border_color=BORDER,
            text_color=TEXT, font=font(11, "bold"),
        ).pack(fill="x", padx=14, pady=(0, 10))
        self._refresh_backups()

    def _refresh_backups(self) -> None:
        try:
            rows = backups.list_backups()
        except (ValueError, sqlite3.Error) as exc:
            self._backup_paths = {}
            self.backup_combo.configure(values=["No backups available"])
            self.backup_combo.set("No backups available")
            self._set_status(str(exc), ok=False)
            return
        self._backup_paths = {row["name"]: row["path"] for row in rows}
        values = list(self._backup_paths) or ["No backups available"]
        self.backup_combo.configure(values=values)
        self.backup_combo.set(values[0])

    def _create_backup(self) -> None:
        try:
            report = backups.create_backup()
        except (ValueError, sqlite3.Error, OSError) as exc:
            self._set_status(str(exc), ok=False)
            return
        self._refresh_backups()
        self._set_status(
            f"Backup verified and saved: {Path(report['path']).name}"
        )

    def _restore_backup(self) -> None:
        if self.confirm_restore.get() != 1:
            self._set_status(
                "Tick the confirmation box before restoring.", ok=False
            )
            return
        path = getattr(self, "_backup_paths", {}).get(self.backup_combo.get())
        if not path:
            self._set_status("Choose a backup file first.", ok=False)
            return
        try:
            report = backups.restore_backup(path)
        except (ValueError, sqlite3.Error, OSError) as exc:
            self._set_status(str(exc), ok=False)
            return
        self.confirm_restore.deselect()
        self._refresh_backups()
        self._refresh_batches()
        self._set_status(
            "Restore complete (integrity OK). Close and relaunch the app. "
            f"Safety copy: {report['safety_backup'] or 'disabled'}."
        )
        if self.on_done is not None:
            self.on_done()

    def close(self) -> None:
        if self.on_done is not None:
            self.on_done()
        super().close()
