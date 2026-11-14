"""
Stage 2 - main application shell.

Layout: sidebar (left, full height) + header (top) + content (right).
Navigation swaps one page frame at a time inside the content area, so the
sidebar and header stay in place and no screen is ever rebuilt from scratch.

Stage 10: the signed-in user's role decides which sidebar entries are built
AND which page keys navigate() will accept. A cashier session therefore has
no way to reach the inventory / recipe / user screens, even through a page's
"open another screen" shortcut button.

Stage 11a: a manager-only "Menu" page registers additional dishes. The page
order in PAGES must match permissions.MANAGER_PAGES, since the sidebar is
built from this registry filtered by the role.

Stage 12: the live "pos" entry now points at the barcode / basket Point of
Sale, and a manager-only "products" page holds the scan-to-sell catalog. The
older ramen screens stay registered as legacy tools so their data and tests
remain intact until they are retired in a later stage.

Stage 13: a twelfth, manager-only "reports" page sits next to POS. Because
the sidebar is built from PAGES filtered by the role, a cashier never sees
it, and navigate() refuses the key if anything tries to open it directly.
"""

from __future__ import annotations

from typing import Any, Callable

import customtkinter as ctk

import database
import permissions
import products as catalog
from components.header import Header
from components.sidebar import Sidebar
from components.theme import CREAM
from pages.calculator import CalculatorView
from pages.dashboard import DashboardView
from pages.history import HistoryView
from pages.ingredients import IngredientsView
from pages.inventory import InventoryView
from pages.menu import MenuView
from pages.monitoring import MonitoringView
from pages.products import ProductsView
from pages.recipe import RecipeView
from pages.reports import ReportsView
from pages.store_pos import PosView
from pages.users import UsersView

# One registry drives the sidebar labels, header titles and page classes.
PAGES: dict[str, dict[str, Any]] = {
    "dashboard": {
        "label": "Dashboard",
        "title": "Dashboard",
        "subtitle": "Live overview from your local database",
        "view": DashboardView,
    },
    "ingredients": {
        "label": "Ingredients",
        "title": "Ingredients",
        "subtitle": "Add, edit, search and monitor ingredients",
        "view": IngredientsView,
    },
    "products": {
        "label": "Products",
        "title": "Products & barcodes",
        "subtitle": "Scan-to-sell catalog with categories and PHP prices - manager only",
        "view": ProductsView,
    },
    "menu": {
        "label": "Menu",
        "title": "Menu / dishes",
        "subtitle": "Add dishes and set their Philippine peso prices - manager only",
        "view": MenuView,
    },
    "recipe": {
        "label": "Recipe",
        "title": "Recipe / bill of materials",
        "subtitle": "Choose a dish, then edit its per-serving quantities",
        "view": RecipeView,
    },
    "calculator": {
        "label": "Calculator",
        "title": "Usage calculator",
        "subtitle": "Check servings against current stock - nothing is deducted",
        "view": CalculatorView,
    },
    "pos": {
        "label": "Point of Sale",
        "title": "Point of sale",
        "subtitle": "Scan barcodes, build a basket, deduct stock and print a PHP receipt",
        "view": PosView,
    },
    "reports": {
        "label": "Sales Reports",
        "title": "Sales reports & Z-read",
        "subtitle": "Daily totals, payment mix, top products and CSV export - manager only",
        "view": ReportsView,
    },
    "inventory": {
        "label": "Inventory",
        "title": "Inventory movements",
        "subtitle": "Record stock-in / stock-out and view live balances",
        "view": InventoryView,
    },
    "history": {
        "label": "History",
        "title": "Inventory history",
        "subtitle": "Search and review every recorded stock movement",
        "view": HistoryView,
    },
    "monitoring": {
        "label": "Monitoring",
        "title": "Stock & expiry monitoring",
        "subtitle": "Low stock, out of stock and expiration alerts",
        "view": MonitoringView,
    },
    "users": {
        "label": "Users",
        "title": "User accounts",
        "subtitle": "Manager and cashier accounts - manager access only",
        "view": UsersView,
    },
}


class AppShell(ctk.CTkFrame):
    """Sidebar + header + swappable content page."""

    def __init__(
        self,
        master: Any,
        user: dict[str, Any],
        on_logout: Callable[[], None],
        on_change_password: Callable[[], None],
        on_exit: Callable[[], None],
    ) -> None:
        super().__init__(master, fg_color=CREAM, corner_radius=0)
        # Stage 10: repositories read this session as their default actor.
        permissions.set_session(user)
        self.user = user
        self.allowed_keys: tuple[str, ...] = permissions.allowed_pages(user)
        self.current_page: ctk.CTkFrame | None = None
        self.active_key = permissions.default_page(user)

        self.grid_columnconfigure(0, minsize=240, weight=0)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, minsize=78, weight=0)
        self.grid_rowconfigure(1, weight=1)

        # A cashier never even gets a button for a management screen.
        nav_items = [
            (key, meta["label"])
            for key, meta in PAGES.items()
            if key in self.allowed_keys
        ]
        self.sidebar = Sidebar(
            self,
            items=nav_items,
            on_navigate=self.navigate,
            menu_item=catalog.STORE_LABEL,
            on_exit=on_exit,
        )
        self.sidebar.grid(row=0, column=0, rowspan=2, sticky="nsew")

        self.header = Header(
            self,
            user=user,
            on_refresh=self.refresh,
            on_logout=on_logout,
            on_change_password=on_change_password,
        )
        self.header.grid(row=0, column=1, sticky="nsew")

        self.content = ctk.CTkFrame(self, fg_color=CREAM, corner_radius=0)
        self.content.grid(row=1, column=1, sticky="nsew")

        self.navigate(self.active_key)

    def navigate(self, key: str) -> None:
        """Switch content page and update sidebar/header state.

        The role check here is the real gate: it also blocks shortcut buttons
        inside pages (e.g. Dashboard -> Monitoring) for a cashier.
        """
        meta = PAGES.get(key)
        if meta is None or key not in self.allowed_keys:
            return
        self.active_key = key
        self.sidebar.set_active(key)
        self.header.set_title(meta["title"], meta["subtitle"])

        if self.current_page is not None:
            self.current_page.destroy()
        self.current_page = meta["view"](
            self.content,
            user=self.user,
            on_navigate=self.navigate,
        )
        self.current_page.pack(fill="both", expand=True)

    def refresh(self) -> None:
        """Re-read the current page from SQLite (used by the Refresh button)."""
        if self.current_page is not None and hasattr(self.current_page, "refresh"):
            self.current_page.refresh()

    def update_user(self, user: dict[str, Any]) -> None:
        """Keep the header in sync after a password change."""
        self.user = user
        self.header.update_user(user)
