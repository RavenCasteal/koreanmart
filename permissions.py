"""
Stage 10 - role-based access rules (pure Python, no SQL, no GUI).

This is the SINGLE source of truth for who may see or do what. Keeping it in
one dependency-free module means the shell, the pages and the repositories
all agree, and the rules are easy to unit-test without a database.

Two roles only, deliberately simple for a one-counter ramen shop:

    manager - sees every screen, manages ingredients, menu dishes, recipe
             (BOM), stock, price, voids and user accounts.
    cashier - sees ONLY the Point of Sale screen; may record a sale and
             (re)print a receipt. Nothing else.

Design notes:
  * Hiding a button is NOT enough. AppShell.navigate() refuses forbidden
    page keys, and repository write functions call require() as a second
    line of defence.
  * `user` is the session dictionary returned by database.verify_credentials().
    A dict with NO 'role' key is treated as a legacy administrator (all
    pre-Stage-10 accounts are managers). An UNKNOWN role string falls back to
    'cashier' (least privilege), never to manager.
  * require() prefers an explicit `actor`. When it is omitted, the live
    login session is checked instead, so a cashier cannot bypass a guard by
    calling a repository without an actor. Only when there is NO session at
    all is the call treated as internal / legacy (which keeps every Stage 1-9
    test and internal helper working unchanged).
  * This is a LOCAL, OFFLINE desktop application. The OS login and physical
    access to the PC are the real security boundary; these rules stop an
    ordinary cashier from wandering into management screens.
"""

from __future__ import annotations

from typing import Any

ROLE_MANAGER = "manager"
ROLE_CASHIER = "cashier"
ROLES: tuple[str, ...] = (ROLE_MANAGER, ROLE_CASHIER)

# Pages the AppShell registry knows about. Order matches components/shell.py.
MANAGER_PAGES: tuple[str, ...] = (
    "dashboard",
    "ingredients",
    "products",
    "menu",
    "recipe",
    "calculator",
    "pos",
    "inventory",
    "history",
    "monitoring",
    "users",
)
CASHIER_PAGES: tuple[str, ...] = ("pos",)

PAGE_ACCESS: dict[str, tuple[str, ...]] = {
    ROLE_MANAGER: MANAGER_PAGES,
    ROLE_CASHIER: CASHIER_PAGES,
}

# Capabilities checked by repository write functions.
CAP_RECORD_SALE = "record_sale"
CAP_PRINT_RECEIPT = "print_receipt"
CAP_VOID_SALE = "void_sale"
CAP_SET_PRICE = "set_price"
CAP_MANAGE_INGREDIENTS = "manage_ingredients"
CAP_MANAGE_RECIPE = "manage_recipe"
CAP_MANAGE_STOCK = "manage_stock"
CAP_MANAGE_USERS = "manage_users"
# Stage 11a: adding / renaming / pricing / deactivating dishes.
CAP_MANAGE_MENU = "manage_menu"
# Stage 12: the barcoded convenience-store product catalog.
CAP_MANAGE_PRODUCTS = "manage_products"

CASHIER_CAPABILITIES: frozenset[str] = frozenset(
    {CAP_RECORD_SALE, CAP_PRINT_RECEIPT}
)
ALL_CAPABILITIES: frozenset[str] = frozenset(
    {
        CAP_RECORD_SALE,
        CAP_PRINT_RECEIPT,
        CAP_VOID_SALE,
        CAP_SET_PRICE,
        CAP_MANAGE_INGREDIENTS,
        CAP_MANAGE_RECIPE,
        CAP_MANAGE_STOCK,
        CAP_MANAGE_USERS,
        CAP_MANAGE_MENU,
        CAP_MANAGE_PRODUCTS,
    }
)

CAPABILITY_LABELS: dict[str, str] = {
    CAP_RECORD_SALE: "record a sale",
    CAP_PRINT_RECEIPT: "print a receipt",
    CAP_VOID_SALE: "void a sale",
    CAP_SET_PRICE: "change the unit price",
    CAP_MANAGE_INGREDIENTS: "manage ingredients",
    CAP_MANAGE_RECIPE: "manage the recipe",
    CAP_MANAGE_STOCK: "record stock movements",
    CAP_MANAGE_USERS: "manage user accounts",
    CAP_MANAGE_MENU: "manage menu dishes",
    CAP_MANAGE_PRODUCTS: "manage products and barcodes",
}


# The signed-in user for the current desktop session. A single-window,
# single-user app needs exactly one. AppShell sets it on login; main.py clears
# it on logout. Repositories fall back to it when a caller omits `actor`, so
# dialogs do not have to thread the user through every constructor.
_SESSION: Any = None


def set_session(user: Any) -> None:
    """Called once when the main shell opens for a signed-in user."""
    global _SESSION
    _SESSION = user


def clear_session() -> None:
    """Called on logout so no stale privileges survive a screen swap."""
    global _SESSION
    _SESSION = None


def current_session() -> Any:
    return _SESSION


class PermissionDenied(ValueError):
    """Raised when the signed-in role is not allowed to do something.

    Subclasses ValueError on purpose: every existing dialog already catches
    (ValueError, sqlite3.Error), so a denial shows up as a normal friendly
    error message instead of crashing a Tk callback.
    """


def normalize_role(value: Any) -> str:
    """Return a valid role, defaulting to the LEAST privileged one."""
    text = str(value if value is not None else "").strip().lower()
    return text if text in ROLES else ROLE_CASHIER


def role_of(user: Any) -> str:
    """Extract the role from a session dict / role string / row-like object."""
    if user is None:
        # No session: legacy internal callers and the pre-Stage-10 tests.
        return ROLE_MANAGER
    if isinstance(user, str):
        return normalize_role(user)
    if isinstance(user, dict):
        value = user.get("role")
    else:
        value = getattr(user, "role", None)
    if value is None:
        # Accounts created before Stage 10 have no role column at all.
        return ROLE_MANAGER
    return normalize_role(value)


def is_manager(user: Any) -> bool:
    return role_of(user) == ROLE_MANAGER


def is_cashier(user: Any) -> bool:
    return role_of(user) == ROLE_CASHIER


def allowed_pages(user: Any) -> tuple[str, ...]:
    """Page keys this user may open, in sidebar order."""
    return PAGE_ACCESS[role_of(user)]


def default_page(user: Any) -> str:
    """First page shown after login (cashiers land straight on POS)."""
    return allowed_pages(user)[0]


def can(user: Any, capability: str) -> bool:
    """True when the user may perform `capability`."""
    if role_of(user) == ROLE_MANAGER:
        return capability in ALL_CAPABILITIES
    return capability in CASHIER_CAPABILITIES


def require(actor: Any, capability: str) -> None:
    """Guard for repository write functions. Raises PermissionDenied.

    `actor` wins when supplied; otherwise the current login session is used.
    If neither exists this is an internal / legacy caller (all Stage 1-9
    tests run this way) and the write proceeds unchanged.
    """
    effective = actor if actor is not None else current_session()
    if effective is None:
        return
    if can(effective, capability):
        return
    action = CAPABILITY_LABELS.get(capability, capability)
    raise PermissionDenied(
        f"Your account is not allowed to {action}. Ask the manager to do it."
    )


def role_label(user: Any) -> str:
    """Short human label for the header / user table."""
    return "Manager" if is_manager(user) else "Cashier"
