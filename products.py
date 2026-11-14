"""
Stage 12 - convenience-store product catalog and barcode data layer.

The shop is NOT a single-dish ramen restaurant: customers can dine in and buy
packaged foods, beverages, sweets, snacks and household goods. Stage 12
therefore replaces the recipe/BOM selling model with PRODUCTS that carry a
barcode, a category and a Philippine-peso retail price.

Design decisions (confirmed by the owner):
  * "Products + stock"  - a product IS a stock item. The existing
    `ingredients` table is reused as the product catalog instead of creating
    a second stock ledger. That keeps the proven stock-in / stock-out,
    inventory history, low-stock and expiry monitoring engine working with
    ZERO changes to those screens.
  * "Scan to ring up"   - a USB barcode scanner behaves like a keyboard: it
    types the digits and presses Enter into the POS lookup field. No barcode
    library, no camera, no network dependency and no new pip package.
  * "Keep users, reset stock" - the accounts (and their PBKDF2 hashes and
    roles) are NEVER touched. The eight ramen demo ingredients are soft-
    retired and a fresh convenience-store catalog is inserted.

This module is deliberately ADDITIVE and SELF-CONTAINED:
  * database.py is not modified. `SCHEMA_VERSION` and its v1-v4 migration
    chain stay exactly as they were, so the whole existing regression suite
    keeps running against fresh temporary databases.
  * Stage 12 stores its own version marker in `app_meta` instead of changing
    `PRAGMA user_version`. Every statement is `IF NOT EXISTS` / idempotent, so
    calling ensure_schema() on every launch is safe.
  * The real database is only ever read or COPIED during verification.

All money is Philippine peso (PHP / ₱). No other currency symbol is used.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta
from typing import Any, Iterable

import database
import permissions

STORE_LABEL = "Neighborhood Mart"
CATALOG_VERSION = "12"
# Stage 15 adds discount columns to store_sales. This is an app_meta marker,
# NOT a PRAGMA user_version bump: database.py / schema.sql stay at v4.
STAGE15_VERSION = "15"

CATEGORIES: tuple[str, ...] = (
    "Ready-to-eat",
    "Food",
    "Beverages",
    "Sweets",
    "Snacks",
    "Household",
    "Other",
)

# Descriptive labels only - there is NO payment gateway. The cashier picks
# how the customer paid; the system just records the label on the receipt.
PAYMENT_METHODS: tuple[str, ...] = ("Cash", "Card", "E-wallet")

_BARCODE_RE = re.compile(r"^[0-9]{6,14}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Additive DDL. Executed one statement at a time (never executescript, which
# would implicitly COMMIT and break the surrounding BEGIN IMMEDIATE).
_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS app_meta (
        key    TEXT PRIMARY KEY,
        value  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS store_sales (
        sale_id             INTEGER PRIMARY KEY AUTOINCREMENT,
        transaction_number  TEXT    NOT NULL UNIQUE,
        transaction_date    TEXT    NOT NULL,
        item_count          INTEGER NOT NULL CHECK (item_count > 0),
        total_amount        REAL    NOT NULL CHECK (total_amount >= 0),
        currency_code       TEXT    NOT NULL DEFAULT 'PHP',
        payment_method      TEXT    NOT NULL CHECK (length(trim(payment_method)) > 0),
        amount_tendered     REAL    NOT NULL CHECK (amount_tendered >= 0),
        change_amount       REAL    NOT NULL DEFAULT 0 CHECK (change_amount >= 0),
        status              TEXT    NOT NULL DEFAULT 'completed'
                            CHECK (status IN ('completed', 'voided')),
        stock_out_id        INTEGER
                            REFERENCES stock_out_transactions (stock_out_id)
                            ON DELETE RESTRICT,
        created_by          INTEGER REFERENCES users (user_id) ON DELETE SET NULL,
        voided_at           TEXT,
        voided_by           INTEGER REFERENCES users (user_id) ON DELETE SET NULL,
        void_reason         TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS store_sale_items (
        sale_item_id     INTEGER PRIMARY KEY AUTOINCREMENT,
        sale_id          INTEGER NOT NULL
                         REFERENCES store_sales (sale_id) ON DELETE CASCADE,
        ingredient_id    INTEGER NOT NULL
                         REFERENCES ingredients (ingredient_id) ON DELETE RESTRICT,
        barcode          TEXT    NOT NULL,
        product_name     TEXT    NOT NULL,
        category         TEXT    NOT NULL,
        unit             TEXT    NOT NULL,
        quantity         REAL    NOT NULL CHECK (quantity > 0),
        unit_price       REAL    NOT NULL CHECK (unit_price >= 0),
        line_total       REAL    NOT NULL CHECK (line_total >= 0),
        balance_before   REAL    NOT NULL CHECK (balance_before >= 0),
        balance_after    REAL    NOT NULL CHECK (balance_after >= 0)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_store_sales_date "
    "ON store_sales (transaction_date)",
    "CREATE INDEX IF NOT EXISTS ix_store_sale_items_sale "
    "ON store_sale_items (sale_id)",
    "CREATE INDEX IF NOT EXISTS ix_store_sale_items_product "
    "ON store_sale_items (ingredient_id)",
)

# Columns added to the existing catalog. ALTER TABLE ... ADD COLUMN is used for
# databases created before Stage 12; fresh databases get them from schema.sql.
_NEW_COLUMNS: tuple[tuple[str, str], ...] = (
    ("barcode", "ALTER TABLE ingredients ADD COLUMN barcode TEXT NOT NULL DEFAULT ''"),
    ("category", "ALTER TABLE ingredients ADD COLUMN category TEXT NOT NULL DEFAULT 'Other'"),
    ("unit_price", "ALTER TABLE ingredients ADD COLUMN unit_price REAL NOT NULL DEFAULT 0 CHECK (unit_price >= 0)"),
    ("is_sellable", "ALTER TABLE ingredients ADD COLUMN is_sellable INTEGER NOT NULL DEFAULT 0 CHECK (is_sellable IN (0, 1))"),
)

# Stage 15 - Senior Citizen / PWD discount snapshots on store_sales.
# Added with ALTER TABLE so an existing real database is upgraded in place.
# Every column has a safe default, so pre-Stage-15 sales remain readable.
_STAGE15_SALES_COLUMNS: tuple[tuple[str, str], ...] = (
    ("subtotal_amount", "ALTER TABLE store_sales ADD COLUMN subtotal_amount REAL NOT NULL DEFAULT 0 CHECK (subtotal_amount >= 0)"),
    ("discount_type", "ALTER TABLE store_sales ADD COLUMN discount_type TEXT NOT NULL DEFAULT ''"),
    ("discount_rate", "ALTER TABLE store_sales ADD COLUMN discount_rate REAL NOT NULL DEFAULT 0 CHECK (discount_rate >= 0)"),
    ("discount_amount", "ALTER TABLE store_sales ADD COLUMN discount_amount REAL NOT NULL DEFAULT 0 CHECK (discount_amount >= 0)"),
    ("customer_name", "ALTER TABLE store_sales ADD COLUMN customer_name TEXT NOT NULL DEFAULT ''"),
    ("customer_id", "ALTER TABLE store_sales ADD COLUMN customer_id TEXT NOT NULL DEFAULT ''"),
)

_INDEX_DDL: tuple[str, ...] = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_ingredients_active_barcode "
    "ON ingredients (barcode COLLATE NOCASE) "
    "WHERE is_active = 1 AND barcode <> ''",
    "CREATE INDEX IF NOT EXISTS ix_ingredients_category "
    "ON ingredients (category)",
)

# (barcode, name, category, unit, price, opening stock, minimum, expiry days)
# The expiry column is a number of days from today, or None for non-perishables.
# Prices are Philippine peso. Quantities are whole retail units.
_SAMPLE_PRODUCTS: tuple[tuple[str, str, str, str, float, int, int, int | None], ...] = (
    ("4800016301016", "Lucky Me! Pancit Canton Original", "Food", "pack", 18.00, 48, 12, None),
    ("4800016301023", "Lucky Me! Chicken Noodles", "Food", "pack", 15.00, 36, 12, None),
    ("4800088001234", "Century Tuna Flakes in Oil", "Food", "can", 62.00, 24, 6, None),
    ("4801234560017", "Gardenia Classic White Bread", "Food", "loaf", 65.00, 12, 4, 4),
    ("4800013234561", "Chicken Siopao (ready to eat)", "Ready-to-eat", "piece", 45.00, 20, 5, 2),
    ("4800014234568", "Pork Siomai 6 pcs", "Ready-to-eat", "pack", 55.00, 15, 4, 2),
    ("4800001234567", "Coca-Cola Original 500 ml", "Beverages", "bottle", 35.00, 60, 12, None),
    ("4800002234564", "Pure Drinking Water 500 ml", "Beverages", "bottle", 15.00, 72, 18, None),
    ("4800003234561", "Nescafe 3in1 Original", "Beverages", "sachet", 12.00, 100, 24, None),
    ("4800004234568", "Milo Ready-to-Drink 200 ml", "Beverages", "box", 28.00, 36, 9, 30),
    ("4800005234565", "Cloud 9 Classic Chocolate", "Sweets", "bar", 12.00, 80, 20, None),
    ("4800006234562", "Choc Nut 8s", "Sweets", "pack", 32.00, 40, 10, None),
    ("4800007234569", "Rebisco Biscuit 10s", "Sweets", "pack", 20.00, 50, 12, 90),
    ("4800008234566", "Piattos Cheese 85 g", "Snacks", "pack", 38.00, 40, 10, 90),
    ("4800009234563", "Nova Cheddar 72 g", "Snacks", "pack", 36.00, 36, 9, 90),
    ("4800010234560", "Boy Bawang Garlic 50 g", "Snacks", "pack", 18.00, 45, 12, 90),
    ("4800011234567", "Safeguard Bath Soap 90 g", "Household", "bar", 32.00, 30, 8, None),
    ("4800012234564", "Facial Tissue 2-ply", "Household", "pack", 45.00, 24, 6, None),
)


# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------
def _rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    """Materialise rows as plain dicts (works with and without row_factory)."""
    return [dict(row) for row in cursor.fetchall()]


def _meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM app_meta WHERE key = ?", (key,)
    ).fetchone()
    return None if row is None else str(row["value"] if isinstance(row, sqlite3.Row) else row[0])


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO app_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def validate_text(value: Any, label: str, *, max_length: int = 80) -> str:
    text = str(value if value is not None else "").strip()
    if not text:
        raise ValueError(f"{label} is required.")
    if len(text) > max_length:
        raise ValueError(f"{label} cannot be longer than {max_length} characters.")
    return text


def validate_barcode(value: Any) -> str:
    """Barcode = 6 to 14 digits (EAN-8, UPC-A, EAN-13). No spaces or dashes."""
    text = str(value if value is not None else "").strip()
    text = text.replace(" ", "").replace("-", "")
    if not _BARCODE_RE.fullmatch(text):
        raise ValueError("Barcode must be 6 to 14 digits (numbers only).")
    return text


def validate_category(value: Any) -> str:
    text = str(value if value is not None else "").strip()
    if text not in CATEGORIES:
        raise ValueError("Choose a valid product category.")
    return text


def validate_money(value: Any, label: str) -> float:
    text = str(value if value is not None else "").strip()
    try:
        number = float(text)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a number.") from None
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError(f"{label} must be a number.")
    if number < 0:
        raise ValueError(f"{label} cannot be negative.")
    return round(number, 2)


def validate_whole(value: Any, label: str, *, allow_zero: bool = True) -> int:
    text = str(value if value is not None else "").strip()
    try:
        number = int(text)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a whole number.") from None
    if number < 0 or (number == 0 and not allow_zero):
        raise ValueError(f"{label} must be {'at least 1' if not allow_zero else '0 or more'}.")
    return number


def validate_expiry(value: Any) -> str | None:
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    if not _DATE_RE.fullmatch(text):
        raise ValueError("Expiration date must be YYYY-MM-DD or blank.")
    try:
        date.fromisoformat(text)
    except ValueError:
        raise ValueError("Expiration date is not a real calendar date.") from None
    return text


# ----------------------------------------------------------------------
# Additive, idempotent schema
# ----------------------------------------------------------------------
def column_names(db_path: Any = None) -> set[str]:
    conn = database.get_connection(db_path)
    try:
        return {str(row[1]) for row in conn.execute("PRAGMA table_info(ingredients)")}
    finally:
        conn.close()


def ensure_schema(db_path: Any = None) -> dict[str, Any]:
    """Create the Stage 12 tables/columns if they are missing.

    Safe to call on every launch and on a brand-new database. Returns a small
    report so the startup path and the tests can assert what happened.
    """
    added: list[str] = []
    added_sale_columns: list[str] = []
    with database.transaction(db_path) as conn:
        for statement in _DDL:
            conn.execute(statement)
        existing = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(ingredients)")
        }
        for name, statement in _NEW_COLUMNS:
            if name not in existing:
                conn.execute(statement)
                added.append(name)
        # Stage 15 keeps its own list so the Stage 12 "exactly four columns"
        # regression test stays valid.
        sales_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(store_sales)")
        }
        for name, statement in _STAGE15_SALES_COLUMNS:
            if name not in sales_columns:
                conn.execute(statement)
                added_sale_columns.append(name)
        for statement in _INDEX_DDL:
            conn.execute(statement)
        _set_meta(conn, "stage12_schema", CATALOG_VERSION)
        _set_meta(conn, "stage15_schema", STAGE15_VERSION)
    return {
        "added_columns": added,
        "added_sale_columns": added_sale_columns,
        "version": CATALOG_VERSION,
        "stage15_version": STAGE15_VERSION,
    }


def catalog_ready(db_path: Any = None) -> bool:
    """True once the convenience-store catalog has been bootstrapped."""
    conn = database.get_connection(db_path)
    try:
        try:
            return _meta(conn, "catalog_seeded") == "1"
        except sqlite3.Error:
            return False
    finally:
        conn.close()


def bootstrap_catalog(db_path: Any = None) -> dict[str, Any]:
    """Replace the ramen demo stock with the convenience-store catalog ONCE.

    Users, password hashes, roles and all sales history are left untouched.
    The old demo records are soft-retired (is_active = 0) rather than deleted,
    so any historical foreign key stays valid. Opening balances are recorded
    as a real stock-in transaction, so Dashboard / History / Monitoring agree
    with the catalog from the very first launch.
    """
    ensure_schema(db_path)
    with database.transaction(db_path) as conn:
        if _meta(conn, "catalog_seeded") == "1":
            return {"seeded": False, "retired": 0, "products": 0}

        timestamp = database.local_timestamp()
        # Always clean out the old ramen demo rows, even if a manager has
        # already created products. Otherwise those unbarcoded rows would
        # linger in the Products table forever and confuse the catalog.
        retired = conn.execute(
            """
            UPDATE ingredients
            SET is_active = 0, is_sellable = 0, quantity_on_hand = 0,
                updated_at = ?
            WHERE is_active = 1 AND barcode = ''
            """,
            (timestamp,),
        ).rowcount

        existing_products = int(
            conn.execute(
                "SELECT COUNT(*) FROM ingredients WHERE barcode <> ''"
            ).fetchone()[0]
        )
        if existing_products:
            # A manager already built a catalog - never overwrite their work.
            _set_meta(conn, "catalog_seeded", "1")
            return {
                "seeded": False,
                "retired": int(retired or 0),
                "products": existing_products,
            }

        in_header = conn.execute(
            """
            INSERT INTO stock_in_transactions
                (transaction_date, reference_no, notes, created_by)
            VALUES (?, ?, ?, NULL)
            """,
            (timestamp, "OPENING", "Stage 12 convenience-store opening balances"),
        )
        stock_in_id = int(in_header.lastrowid)
        today = date.today()

        for code, name, category, unit, price, opening, minimum, days in _SAMPLE_PRODUCTS:
            expiry = (
                (today + timedelta(days=days)).isoformat() if days is not None else None
            )
            cursor = conn.execute(
                """
                INSERT INTO ingredients
                    (name, unit, quantity_on_hand, minimum_stock,
                     expiration_date, is_active, created_at, updated_at,
                     barcode, category, unit_price, is_sellable)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, 1)
                """,
                (
                    name, unit, float(opening), float(minimum), expiry,
                    timestamp, timestamp, code, category, price,
                ),
            )
            product_id = int(cursor.lastrowid)
            conn.execute(
                """
                INSERT INTO stock_in_details
                    (stock_in_id, ingredient_id, quantity, unit,
                     expiration_date, notes)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (stock_in_id, product_id, float(opening), unit, expiry,
                 "Opening balance"),
            )

        _set_meta(conn, "catalog_seeded", "1")
    return {
        "seeded": True,
        "retired": int(retired or 0),
        "products": len(_SAMPLE_PRODUCTS),
        "stock_in_id": stock_in_id,
    }


# ----------------------------------------------------------------------
# Reads
# ----------------------------------------------------------------------
def list_products(
    search: str = "",
    category: str = "",
    db_path: Any = None,
    include_inactive: bool = False,
) -> list[dict[str, Any]]:
    """Products for the catalog table, optionally filtered by text/category."""
    ensure_schema(db_path)
    clauses: list[str] = []
    params: list[Any] = []
    if not include_inactive:
        clauses.append("is_active = 1")
    needle = str(search or "").strip()
    if needle:
        clauses.append("(name LIKE ? OR barcode LIKE ? OR category LIKE ?)")
        params.extend([f"%{needle}%"] * 3)
    chosen = str(category or "").strip()
    if chosen and chosen != "All":
        clauses.append("category = ?")
        params.append(chosen)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    conn = database.get_connection(db_path)
    try:
        cursor = conn.execute(
            "SELECT ingredient_id, name, barcode, category, unit, unit_price, "
            "quantity_on_hand, minimum_stock, expiration_date, is_active, "
            "is_sellable, updated_at "
            f"FROM ingredients{where} "
            "ORDER BY category, name",
            params,
        )
        return _rows(cursor)
    finally:
        conn.close()


def find_by_barcode(barcode: Any, db_path: Any = None) -> dict[str, Any] | None:
    """Exact lookup used by the scanner. Only ACTIVE, SELLABLE products match."""
    ensure_schema(db_path)
    code = str(barcode if barcode is not None else "").strip()
    if not code:
        return None
    conn = database.get_connection(db_path)
    try:
        cursor = conn.execute(
            "SELECT ingredient_id, name, barcode, category, unit, unit_price, "
            "quantity_on_hand, is_sellable "
            "FROM ingredients "
            "WHERE barcode = ? AND is_active = 1 AND is_sellable = 1",
            (code,),
        )
        rows = _rows(cursor)
        return rows[0] if rows else None
    finally:
        conn.close()


def search_sellable(search: str = "", db_path: Any = None) -> list[dict[str, Any]]:
    """Fallback picker when a barcode is damaged or a product has no label."""
    ensure_schema(db_path)
    needle = str(search or "").strip()
    conn = database.get_connection(db_path)
    try:
        if needle:
            cursor = conn.execute(
                "SELECT ingredient_id, name, barcode, category, unit, unit_price, "
                "quantity_on_hand FROM ingredients "
                "WHERE is_active = 1 AND is_sellable = 1 "
                "AND (name LIKE ? OR barcode LIKE ?) "
                "ORDER BY name LIMIT 40",
                (f"%{needle}%", f"%{needle}%"),
            )
        else:
            cursor = conn.execute(
                "SELECT ingredient_id, name, barcode, category, unit, unit_price, "
                "quantity_on_hand FROM ingredients "
                "WHERE is_active = 1 AND is_sellable = 1 "
                "ORDER BY name LIMIT 40",
            )
        return _rows(cursor)
    finally:
        conn.close()


def categories(db_path: Any = None) -> list[str]:
    conn = database.get_connection(db_path)
    try:
        try:
            rows = _rows(conn.execute(
                "SELECT DISTINCT category FROM ingredients "
                "WHERE is_active = 1 AND barcode <> '' ORDER BY category"
            ))
            return [str(row["category"]) for row in rows]
        except sqlite3.Error:
            return list(CATEGORIES)
    finally:
        conn.close()


def summary(db_path: Any = None) -> dict[str, Any]:
    """Small cards on the Products page - everything comes from SQLite."""
    ensure_schema(db_path)
    conn = database.get_connection(db_path)
    try:
        cursor = conn.execute(
            "SELECT COUNT(*) AS total, "
            "COALESCE(SUM(CASE WHEN quantity_on_hand <= minimum_stock "
            "             THEN 1 ELSE 0 END), 0) AS low, "
            "COALESCE(SUM(CASE WHEN quantity_on_hand = 0 THEN 1 ELSE 0 END), 0) AS out "
            "FROM ingredients WHERE is_active = 1 AND barcode <> ''"
        )
        row = _rows(cursor)[0]
        return {
            "products": int(row["total"]),
            "low_stock": int(row["low"]),
            "out_of_stock": int(row["out"]),
            "categories": len(categories(db_path)),
        }
    finally:
        conn.close()


# ----------------------------------------------------------------------
# Writes (manager only)
# ----------------------------------------------------------------------
def _check_duplicates(
    conn: sqlite3.Connection, name: str, barcode: str, exclude_id: int | None
) -> None:
    row = conn.execute(
        "SELECT ingredient_id FROM ingredients "
        "WHERE barcode = ? AND is_active = 1 AND ingredient_id IS NOT ?",
        (barcode, exclude_id),
    ).fetchone()
    if row:
        raise ValueError("That barcode is already used by another active product.")
    row = conn.execute(
        "SELECT ingredient_id FROM ingredients "
        "WHERE name = ? COLLATE NOCASE AND is_active = 1 "
        "AND ingredient_id IS NOT ?",
        (name, exclude_id),
    ).fetchone()
    if row:
        raise ValueError("An active product with that name already exists.")


def add_product(
    name: Any,
    barcode: Any,
    category: Any,
    unit: Any,
    unit_price: Any,
    minimum_stock: Any = 0,
    expiration_date: Any = "",
    opening_stock: Any = 0,
    db_path: Any = None,
    actor: Any = None,
) -> int:
    """Add a product. Opening stock is recorded as a real stock-in movement."""
    permissions.require(actor, permissions.CAP_MANAGE_PRODUCTS)
    ensure_schema(db_path)
    clean_name = validate_text(name, "Product name")
    code = validate_barcode(barcode)
    clean_category = validate_category(category)
    clean_unit = validate_text(unit, "Unit", max_length=20)
    price = validate_money(unit_price, "Unit price")
    minimum = validate_whole(minimum_stock, "Minimum stock")
    opening = validate_whole(opening_stock, "Opening stock")
    expiry = validate_expiry(expiration_date)
    timestamp = database.local_timestamp()

    with database.transaction(db_path) as conn:
        _check_duplicates(conn, clean_name, code, None)
        cursor = conn.execute(
            """
            INSERT INTO ingredients
                (name, unit, quantity_on_hand, minimum_stock, expiration_date,
                 is_active, created_at, updated_at,
                 barcode, category, unit_price, is_sellable)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, 1)
            """,
            (
                clean_name, clean_unit, float(opening), float(minimum), expiry,
                timestamp, timestamp, code, clean_category, price,
            ),
        )
        product_id = int(cursor.lastrowid)
        if opening:
            header = conn.execute(
                """
                INSERT INTO stock_in_transactions
                    (transaction_date, reference_no, notes, created_by)
                VALUES (?, ?, ?, NULL)
                """,
                (timestamp, "OPENING", f"Opening balance for {clean_name}"),
            )
            conn.execute(
                """
                INSERT INTO stock_in_details
                    (stock_in_id, ingredient_id, quantity, unit,
                     expiration_date, notes)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    int(header.lastrowid), product_id, float(opening),
                    clean_unit, expiry, "Opening balance",
                ),
            )
    return product_id


def update_product(
    ingredient_id: int,
    name: Any,
    barcode: Any,
    category: Any,
    unit: Any,
    unit_price: Any,
    minimum_stock: Any = 0,
    expiration_date: Any = "",
    db_path: Any = None,
    actor: Any = None,
) -> dict[str, Any]:
    """Edit catalog fields only. NEVER changes quantity_on_hand."""
    permissions.require(actor, permissions.CAP_MANAGE_PRODUCTS)
    ensure_schema(db_path)
    product_id = int(ingredient_id)
    clean_name = validate_text(name, "Product name")
    code = validate_barcode(barcode)
    clean_category = validate_category(category)
    clean_unit = validate_text(unit, "Unit", max_length=20)
    price = validate_money(unit_price, "Unit price")
    minimum = validate_whole(minimum_stock, "Minimum stock")
    expiry = validate_expiry(expiration_date)
    timestamp = database.local_timestamp()

    with database.transaction(db_path) as conn:
        row = conn.execute(
            "SELECT ingredient_id FROM ingredients WHERE ingredient_id = ? "
            "AND is_active = 1",
            (product_id,),
        ).fetchone()
        if row is None:
            raise ValueError("That product no longer exists.")
        _check_duplicates(conn, clean_name, code, product_id)
        conn.execute(
            """
            UPDATE ingredients
            SET name = ?, barcode = ?, category = ?, unit = ?, unit_price = ?,
                minimum_stock = ?, expiration_date = ?, updated_at = ?
            WHERE ingredient_id = ?
            """,
            (
                clean_name, code, clean_category, clean_unit, price,
                float(minimum), expiry, timestamp, product_id,
            ),
        )
    return {"ingredient_id": product_id, "name": clean_name, "barcode": code}


def deactivate_product(
    ingredient_id: int, db_path: Any = None, actor: Any = None
) -> dict[str, Any]:
    """Soft-retire a product. History keeps its foreign key intact."""
    permissions.require(actor, permissions.CAP_MANAGE_PRODUCTS)
    ensure_schema(db_path)
    product_id = int(ingredient_id)
    timestamp = database.local_timestamp()
    with database.transaction(db_path) as conn:
        row = conn.execute(
            "SELECT name FROM ingredients WHERE ingredient_id = ? AND is_active = 1",
            (product_id,),
        ).fetchone()
        if row is None:
            raise ValueError("That product is already retired.")
        name = str(row[0])
        conn.execute(
            "UPDATE ingredients SET is_active = 0, is_sellable = 0, updated_at = ? "
            "WHERE ingredient_id = ?",
            (timestamp, product_id),
        )
    return {"ingredient_id": product_id, "name": name, "active": False}
