"""
Ramen Inventory System - database layer
=======================================
All SQLite access lives in this module. The user interface (pages/,
components/) must never write SQL directly.

Stage 1 responsibilities:
    * create the database folder and file on first launch
    * run schema.sql (Stage 9: 11 tables + 1 view, with primary/foreign keys)
    * seed the single menu item, its recipe and the eight ingredients
    * seed one administrator with a securely hashed password
    * provide login / logout support and password change

Stage 9 added the POS tables. initialize_database() now also runs an
IDEMPOTENT, NON-DESTRUCTIVE migration so an existing database (including one
whose admin password was already changed) gains the new tables/column without
ever being reseeded or reset.

Stage 10 added users.role (manager / cashier) the same way: one ALTER TABLE
ADD COLUMN, guarded by a column check. Seeding is STILL keyed to version < 1,
so bumping SCHEMA_VERSION never recreates accounts or resets a password.

Stage 11a replaces the "only one menu item ever" CHECK with a rule that allows
many dishes but keeps exactly ONE primary POS dish. SQLite cannot DROP a CHECK
constraint, so pre-v4 databases get a carefully guarded TABLE REBUILD: all
rows, ids, prices and the admin's password hash are preserved, foreign keys
are verified afterwards, and the whole rebuild is one atomic transaction.
A fresh database already has the v4 definition, so it is never rebuilt.

Python 3.13 / standard library only (sqlite3, hashlib, hmac, os).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from permissions import ROLE_MANAGER
from stock_rules import stock_status

# ------------------------------------------------------------------
# Paths and constants
# ------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DB_DIR = BASE_DIR / "database"
DB_PATH = DB_DIR / "ramen_inventory.db"
SCHEMA_PATH = DB_DIR / "schema.sql"

# 1 = original build, 2 = Stage 9 POS tables, 3 = Stage 10 roles,
# 4 = Stage 11a multiple dishes (one primary POS dish).
# IMPORTANT: seeding is keyed to version < 1, NOT to SCHEMA_VERSION, so
# raising this never re-seeds an existing database and never overwrites the
# administrator's changed password.
SCHEMA_VERSION = 4

# Philippine peso (PHP). Starting unit price for one Regular Ramen; editable
# on the POS screen. Kept here so schema.sql, the migration and tests agree.
DEFAULT_UNIT_PRICE = 150.0

PBKDF2_ITERATIONS = 200_000
MIN_PASSWORD_LENGTH = 8

# First-run credentials. The app FORCES a password change on first login.
DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "RamenAdmin2026!"

MENU_ITEM_NAME = "Regular Ramen"

# name, unit, quantity per serving, minimum stock (2 servings worth)
# Available stock is intentionally seeded as 0 - it is filled by stock-in
# transactions in Stage 5, never invented here.
INITIAL_INGREDIENTS: tuple[tuple[str, str, float, float], ...] = (
    ("Ramen noodles", "grams", 150.0, 300.0),
    ("Pork", "grams", 100.0, 200.0),
    ("Ramen broth", "ml", 400.0, 800.0),
    ("Egg", "piece", 1.0, 2.0),
    ("Nori sheets", "sheet", 1.0, 2.0),
    ("Green onions", "grams", 10.0, 20.0),
    ("Soy sauce", "ml", 15.0, 30.0),
    ("Garlic", "grams", 5.0, 10.0),
)


def local_timestamp() -> str:
    """Return the current LOCAL time as sortable ISO-like text.

    SQLite has no date type, so every timestamp is stored as TEXT in the
    format YYYY-MM-DD HH:MM:SS. This keeps DB Browser for SQLite readable.
    """
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------------
# Connections and transactions
# ------------------------------------------------------------------
def get_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Open a SQLite connection with the required safety pragmas.

    IMPORTANT: SQLite disables foreign key enforcement by default. The
    pragma must be set on EVERY connection, including connections opened
    later by other modules.
    """
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def transaction(db_path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    """Run a group of writes atomically (all succeed or all roll back).

    BEGIN IMMEDIATE takes the write lock up front, which prevents a second
    process from changing stock between our read and our update.
    """
    conn = get_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ------------------------------------------------------------------
# Password security (no third-party dependency)
# ------------------------------------------------------------------
def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    """Hash a password with PBKDF2-HMAC-SHA256 and a fresh random salt.

    Stored format: pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>
    Keeping the algorithm and iteration count in the value lets us upgrade
    hashing later without breaking existing accounts.
    """
    if not isinstance(password, str) or not password:
        raise ValueError("Password must be a non-empty string.")
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations
    )
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Constant-time comparison of a password against a stored hash."""
    try:
        algorithm, iterations_text, salt_hex, digest_hex = stored_hash.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        candidate = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iterations_text),
        )
        return hmac.compare_digest(candidate.hex(), digest_hex)
    except (AttributeError, TypeError, ValueError):
        return False


def validate_password_strength(password: str) -> None:
    """Raise ValueError with a user-friendly message for weak passwords."""
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters long."
        )
    if not any(char.isalpha() for char in password) or not any(
        char.isdigit() for char in password
    ):
        raise ValueError("Password must contain at least one letter and one number.")


# Used to keep the login timing similar for unknown usernames.
_DUMMY_HASH = hash_password("unknown-account-timing-placeholder")


# ------------------------------------------------------------------
# First-launch initialization and seed data
# ------------------------------------------------------------------
def _table_columns(
    conn: sqlite3.Connection, table: str
) -> set[str]:
    """Column names of a table (used only for safe, idempotent migrations)."""
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_legacy_columns(conn: sqlite3.Connection) -> None:
    """Add Stage 9 / 10 columns to databases created before those stages.

    SQLite's ALTER TABLE ADD COLUMN cannot add a table, but schema.sql already
    created the new tables with CREATE TABLE IF NOT EXISTS. Only columns on
    EXISTING tables need this. Every statement is guarded by a column check,
    so running this on every launch is harmless. Existing rows receive the
    documented defaults (150.0 pesos, 'manager' role); no row is deleted or
    rewritten and no password hash is ever touched.
    """
    if "unit_price" not in _table_columns(conn, "menu_items"):
        conn.execute(
            "ALTER TABLE menu_items "
            "ADD COLUMN unit_price REAL NOT NULL DEFAULT 150.0"
        )
    if "role" not in _table_columns(conn, "users"):
        conn.execute(
            "ALTER TABLE users ADD COLUMN role TEXT NOT NULL "
            "DEFAULT 'manager' CHECK (role IN ('manager', 'cashier'))"
        )


# Stage 11a: the v4 menu_items definition. Kept here (not only in schema.sql)
# because existing databases must be rebuilt to drop the old CHECK constraint.
_MENU_ITEMS_V4_DDL = """
CREATE TABLE menu_items_stage11a (
    menu_item_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    description   TEXT,
    is_active     INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    singleton     INTEGER NOT NULL DEFAULT 1 CHECK (singleton IN (0, 1)),
    unit_price    REAL    NOT NULL DEFAULT 150.0,
    created_at    TEXT    NOT NULL
)
"""


def _menu_items_has_legacy_check(conn: sqlite3.Connection) -> bool:
    """Detect a pre-Stage-11a menu_items table by its one-dish CHECK.

    Reading sqlite_master is the only reliable way to tell an old table from
    a new one. This makes the migration IDEMPOTENT: once rebuilt, a later
    launch sees no legacy CHECK and does nothing at all (so the file hash
    stays stable).
    """
    row = conn.execute(
        """
        SELECT sql FROM sqlite_master
        WHERE type = 'table' AND name = 'menu_items'
        """
    ).fetchone()
    return row is not None and "singleton = 1" in str(row[0])


def _migrate_menu_items_to_v4(conn: sqlite3.Connection) -> None:
    """Allow several dishes while keeping EXACTLY ONE primary POS dish.

    SQLite cannot DROP or ALTER a CHECK constraint. The only safe route is a
    table rebuild. Everything happens in ONE explicit transaction, so a
    failure rolls back to the original database. Foreign keys are switched
    off only for the swap (recipes.menu_item_id points at this table), then
    immediately re-enabled and verified with PRAGMA foreign_key_check.

    Nothing is reseeded, nothing is deleted, no password hash is touched,
    and every menu_item_id is copied unchanged so recipes keep their links.
    """
    if not _menu_items_has_legacy_check(conn):
        return

    # PRAGMA foreign_keys is a no-op inside a transaction, so commit first.
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA legacy_alter_table = ON")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # schema.sql may already have created the v4 index on the OLD
            # table on this launch; drop both names to avoid a name clash.
            conn.execute("DROP INDEX IF EXISTS ux_menu_items_single")
            conn.execute("DROP INDEX IF EXISTS ux_menu_items_primary")
            conn.execute("DROP TABLE IF EXISTS menu_items_stage11a")
            conn.execute(_MENU_ITEMS_V4_DDL)
            conn.execute(
                """
                INSERT INTO menu_items_stage11a
                    (menu_item_id, name, description, is_active, singleton,
                     unit_price, created_at)
                SELECT menu_item_id, name, description, is_active, singleton,
                       unit_price, created_at
                FROM menu_items
                """
            )
            conn.execute("DROP TABLE menu_items")
            # legacy_alter_table = ON stops SQLite from rewriting the
            # REFERENCES clause inside recipes (it already says "menu_items").
            conn.execute(
                "ALTER TABLE menu_items_stage11a RENAME TO menu_items"
            )
            conn.execute(
                "CREATE UNIQUE INDEX ux_menu_items_primary "
                "ON menu_items (singleton) WHERE singleton = 1"
            )
            broken = conn.execute("PRAGMA foreign_key_check").fetchall()
            if broken:
                raise sqlite3.IntegrityError(
                    "Stage 11a migration found broken foreign keys; "
                    "the rebuild was rolled back."
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.execute("PRAGMA legacy_alter_table = OFF")
        conn.execute("PRAGMA foreign_keys = ON")


def initialize_database(db_path: str | Path | None = None) -> Path:
    """Create the database, run the schema and seed data exactly once.

    Safe to call on every launch. Seeding is gated on user_version < 1 (never
    on SCHEMA_VERSION), so an admin's changed password is never overwritten.
    The Stage 9/10 column migrations are idempotent and only ADD columns;
    the Stage 11a table rebuild runs once and is detected from sqlite_master.
    """
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")

    conn = get_connection(path)
    try:
        # executescript() commits implicitly, so run it before seeding.
        conn.executescript(schema_sql)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            with conn:
                _seed_initial_data(conn)
        # Always run: a pre-Stage-9 database needs the new column, and a fresh
        # one already has it (the guard makes this a no-op).
        with conn:
            _migrate_legacy_columns(conn)
        # Stage 11a: rebuild only databases that still carry the old
        # "ONLY ONE DISH EVER" CHECK. Fresh databases are already v4.
        _migrate_menu_items_to_v4(conn)
        if version < SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()
        return path
    finally:
        conn.close()


def _seed_initial_data(conn: sqlite3.Connection) -> None:
    """Insert the one menu item, its recipe and the eight ingredients."""
    now = local_timestamp()

    conn.execute(
        """
        INSERT INTO users
            (username, password_hash, full_name, role, is_active,
             must_change_password, created_at)
        VALUES (?, ?, ?, ?, 1, 1, ?)
        """,
        (
            DEFAULT_ADMIN_USERNAME,
            hash_password(DEFAULT_ADMIN_PASSWORD),
            "System Administrator",
            ROLE_MANAGER,
            now,
        ),
    )

    conn.execute(
        """
        INSERT INTO menu_items (name, description, created_at)
        VALUES (?, ?, ?)
        """,
        (MENU_ITEM_NAME, "The only menu item in this system.", now),
    )
    menu_item_id = conn.execute(
        "SELECT menu_item_id FROM menu_items WHERE name = ?", (MENU_ITEM_NAME,)
    ).fetchone()["menu_item_id"]

    conn.execute(
        """
        INSERT INTO recipes (menu_item_id, notes, updated_at)
        VALUES (?, ?, ?)
        """,
        (menu_item_id, "Sample recipe - quantities are editable.", now),
    )
    recipe_id = conn.execute(
        "SELECT recipe_id FROM recipes WHERE menu_item_id = ?", (menu_item_id,)
    ).fetchone()["recipe_id"]

    for name, unit, per_serving, minimum_stock in INITIAL_INGREDIENTS:
        conn.execute(
            """
            INSERT INTO ingredients
                (name, unit, quantity_on_hand, minimum_stock, expiration_date,
                 is_active, created_at, updated_at)
            VALUES (?, ?, 0, ?, NULL, 1, ?, ?)
            """,
            (name, unit, minimum_stock, now, now),
        )
        ingredient_id = conn.execute(
            "SELECT ingredient_id FROM ingredients WHERE name = ?", (name,)
        ).fetchone()["ingredient_id"]
        conn.execute(
            """
            INSERT INTO recipe_ingredients
                (recipe_id, ingredient_id, quantity_per_serving)
            VALUES (?, ?, ?)
            """,
            (recipe_id, ingredient_id, per_serving),
        )


# ------------------------------------------------------------------
# Authentication
# ------------------------------------------------------------------
def verify_credentials(
    username: str, password: str, db_path: str | Path | None = None
) -> dict[str, Any] | None:
    """Return the user row (without the hash) on success, else None."""
    if not isinstance(username, str) or not isinstance(password, str):
        return None

    conn = get_connection(db_path)
    try:
        row = conn.execute(
            """
            SELECT * FROM users
            WHERE username = ? COLLATE NOCASE AND is_active = 1
            """,
            (username.strip(),),
        ).fetchone()

        if row is None:
            verify_password(password, _DUMMY_HASH)
            return None
        if not verify_password(password, row["password_hash"]):
            return None

        login_time = local_timestamp()
        conn.execute(
            "UPDATE users SET last_login_at = ? WHERE user_id = ?",
            (login_time, row["user_id"]),
        )
        conn.commit()
        # The row was read before the UPDATE, so refresh that one field.
        user = {key: row[key] for key in row.keys() if key != "password_hash"}
        user["last_login_at"] = login_time
        # Stage 10: every session carries its role. The fallback keeps a
        # pre-migration row readable, but a real session always has a role.
        user.setdefault("role", ROLE_MANAGER)
        return user
    finally:
        conn.close()


def change_password(
    user_id: int,
    current_password: str,
    new_password: str,
    db_path: str | Path | None = None,
) -> None:
    """Verify the old password, then store a fresh hash of the new one."""
    validate_password_strength(new_password)
    with transaction(db_path) as conn:
        row = conn.execute(
            "SELECT password_hash FROM users WHERE user_id = ? AND is_active = 1",
            (user_id,),
        ).fetchone()
        if row is None:
            raise ValueError("User account was not found.")
        if not verify_password(current_password, row["password_hash"]):
            raise ValueError("Current password is incorrect.")
        if verify_password(new_password, row["password_hash"]):
            raise ValueError("New password must be different from the current one.")
        conn.execute(
            """
            UPDATE users
            SET password_hash = ?, must_change_password = 0
            WHERE user_id = ?
            """,
            (hash_password(new_password), user_id),
        )


def default_credentials_active(db_path: str | Path | None = None) -> bool:
    """True ONLY while the seeded admin still uses its first-run password.

    Stage 10 fix: this used to be "any account with must_change_password = 1".
    Once cashier accounts exist, that would keep printing the admin's default
    password on the shared counter login screen. Checking the actual stored
    hash instead means the hint disappears the moment the admin changes it,
    and never reappears because a cashier still has to set a new password.
    """
    with closing(get_connection(db_path)) as conn:
        row = conn.execute(
            """
            SELECT password_hash FROM users
            WHERE username = ? COLLATE NOCASE AND is_active = 1
            """,
            (DEFAULT_ADMIN_USERNAME,),
        ).fetchone()
        if row is None:
            return False
        return verify_password(DEFAULT_ADMIN_PASSWORD, row["password_hash"])


# ------------------------------------------------------------------
# Small read helpers (used by the Stage 1 verification screen)
# ------------------------------------------------------------------
def get_system_snapshot(db_path: str | Path | None = None) -> dict[str, Any]:
    """Return live counts straight from SQLite - never hardcoded values."""
    path = Path(db_path) if db_path else DB_PATH
    with closing(get_connection(path)) as conn:
        def count(sql: str) -> int:
            return conn.execute(sql).fetchone()[0]

        return {
            "database_file": str(path),
            "menu_items": count("SELECT COUNT(*) FROM menu_items"),
            "recipes": count("SELECT COUNT(*) FROM recipes"),
            "ingredients": count(
                "SELECT COUNT(*) FROM ingredients WHERE is_active = 1"
            ),
            "recipe_lines": count("SELECT COUNT(*) FROM recipe_ingredients"),
            "users": count("SELECT COUNT(*) FROM users WHERE is_active = 1"),
            "stock_movements": count("SELECT COUNT(*) FROM vw_stock_movements"),
            "low_stock": count(
                """
                SELECT COUNT(*) FROM ingredients
                WHERE is_active = 1 AND minimum_stock > 0
                  AND quantity_on_hand > 0
                  AND quantity_on_hand <= minimum_stock
                """
            ),
            "out_of_stock": count(
                """
                SELECT COUNT(*) FROM ingredients
                WHERE is_active = 1 AND quantity_on_hand = 0
                """
            ),
        }


# ------------------------------------------------------------------
# Stage 2 - dashboard queries (every value comes from SQLite)
# ------------------------------------------------------------------
def get_menu_item_name(db_path: str | Path | None = None) -> str:
    """Return the single registered menu item, e.g. 'Regular Ramen'."""
    with closing(get_connection(db_path)) as conn:
        row = conn.execute(
            """
            SELECT name FROM menu_items
            WHERE is_active = 1 ORDER BY menu_item_id LIMIT 1
            """
        ).fetchone()
        return row["name"] if row else "No menu item"


def _ingredient_status(quantity: float, minimum: float) -> str:
    """Dashboard wrapper around the one shared rule (Stage 8 deduplication).

    Out-of-stock and low-stock are mutually exclusive: a zero balance is
    counted only as out of stock.
    """
    return stock_status(quantity, minimum)


def get_dashboard_data(
    db_path: str | Path | None = None, activity_limit: int = 5
) -> dict[str, Any]:
    """Read all dashboard values in one consistent SQLite snapshot.

    Returns: menu_item, total_ingredients, low_stock, out_of_stock,
    recent_activities (list of movement rows) and ingredients (status list).
    """
    with closing(get_connection(db_path)) as conn:
        menu_item = conn.execute(
            """
            SELECT name FROM menu_items
            WHERE is_active = 1 ORDER BY menu_item_id LIMIT 1
            """
        ).fetchone()

        total = conn.execute(
            "SELECT COUNT(*) AS n FROM ingredients WHERE is_active = 1"
        ).fetchone()["n"]
        low = conn.execute(
            """
            SELECT COUNT(*) AS n FROM ingredients
            WHERE is_active = 1 AND minimum_stock > 0
              AND quantity_on_hand > 0
              AND quantity_on_hand <= minimum_stock
            """
        ).fetchone()["n"]
        out = conn.execute(
            """
            SELECT COUNT(*) AS n FROM ingredients
            WHERE is_active = 1 AND quantity_on_hand = 0
            """
        ).fetchone()["n"]

        activities = [
            dict(row)
            for row in conn.execute(
                """
                SELECT movement_type, transaction_date, ingredient_name,
                       unit, quantity, reason, notes
                FROM vw_stock_movements
                ORDER BY transaction_date DESC, detail_id DESC
                LIMIT ?
                """,
                (activity_limit,),
            ).fetchall()
        ]

        ingredients: list[dict[str, Any]] = []
        for row in conn.execute(
            """
            SELECT name, unit, quantity_on_hand, minimum_stock
            FROM ingredients WHERE is_active = 1
            ORDER BY name
            """
        ).fetchall():
            item = dict(row)
            item["status"] = _ingredient_status(
                item["quantity_on_hand"], item["minimum_stock"]
            )
            ingredients.append(item)

        return {
            "menu_item": menu_item["name"] if menu_item else "No menu item",
            "total_ingredients": total,
            "low_stock": low,
            "out_of_stock": out,
            "recent_activities": activities,
            "ingredients": ingredients,
        }
