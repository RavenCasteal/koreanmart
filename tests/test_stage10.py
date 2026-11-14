"""
Stage 10 automated checks - manager / cashier role-based access.

Run from the project folder:
    python -m unittest discover -s tests -v

The concern this stage answers: there is ONE counter PC. The manager must see
all inventory functionality; the person taking orders must NOT. These tests
protect:

  * a single, testable source of truth for roles, page access and capabilities
  * least privilege: unknown roles become cashiers, legacy accounts (no role
    column) remain managers
  * the v3 migration adds users.role WITHOUT reseeding, resetting the admin
    password, or touching stock / recipe / sales data
  * login returns the role, inactive accounts cannot sign in, and a new
    cashier is forced to set their own password
  * account management is manager-only at EVERY layer (writes AND reads)
  * nobody can deactivate themselves or the last active manager
  * hidden buttons are backed by AppShell.navigate() guards and repository
    guards, so a cashier cannot write by skipping the UI
  * the Stage 9 "Exact total" bug stays fixed
  * money is still Philippine peso (PHP / the peso sign)

Every test uses a temporary SQLite database. The real
database/ramen_inventory.db is only ever opened READ-ONLY here. The global
permissions session is cleared before and after every test so no test leaks
a cashier identity into the Stage 1-9 suites. No GUI is opened here.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any

import database
import formatting
import permissions
import recipe_repository as recipe
import repository
import sales_repository as sales
import stock_repository as stock
import users_repository as users
from components.shell import PAGES

ROOT = Path(__file__).resolve().parents[1]
REAL_DB = ROOT / "database" / "ramen_inventory.db"

# Session dictionaries shaped exactly like database.verify_credentials() rows.
MANAGER: dict[str, Any] = {
    "user_id": 1, "username": "admin", "role": permissions.ROLE_MANAGER,
}
CASHIER: dict[str, Any] = {
    "user_id": 99, "username": "counter", "role": permissions.ROLE_CASHIER,
}
OTHER_MANAGER: dict[str, Any] = {
    "user_id": 500, "username": "owner", "role": permissions.ROLE_MANAGER,
}
TEMP_PASSWORD = "Counter2026!"          # >= 8 chars, letter + digit
NEW_ADMIN_PASSWORD = "NewAdmin2026!"     # used only inside temp databases


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


class _SessionIsolation(unittest.TestCase):
    """Base: never leak a fake login into another test module."""

    def setUp(self) -> None:
        permissions.clear_session()
        self.addCleanup(permissions.clear_session)


class _TempDB(_SessionIsolation):
    """Shared temp-database helpers. Contains no test methods itself."""

    def setUp(self) -> None:
        super().setUp()
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.db_path = Path(temp_dir.name) / "ramen_inventory.db"
        database.initialize_database(self.db_path)

    def _hash(self) -> str:
        return hashlib.sha256(self.db_path.read_bytes()).hexdigest()

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
        with closing(database.get_connection(self.db_path)) as conn:
            return list(conn.execute(sql, params))

    def _create_cashier(self, username: str = "counter") -> int:
        return users.create_user(
            username, TEMP_PASSWORD, "Counter Staff",
            permissions.ROLE_CASHIER,
            db_path=self.db_path, actor=MANAGER,
        )


# ----------------------------------------------------------------------
# 1. Pure permission rules (no SQL, no GUI)
# ----------------------------------------------------------------------
class PermissionsRulesTests(_SessionIsolation):

    def test_exactly_two_roles_exist(self) -> None:
        self.assertEqual(
            permissions.ROLES,
            (permissions.ROLE_MANAGER, permissions.ROLE_CASHIER),
        )
        self.assertEqual(set(permissions.PAGE_ACCESS), set(permissions.ROLES))

    def test_manager_sees_all_ten_pages_cashier_sees_only_pos(self) -> None:
        self.assertEqual(
            permissions.allowed_pages(MANAGER), permissions.MANAGER_PAGES
        )
        self.assertEqual(len(permissions.allowed_pages(MANAGER)), 12)
        self.assertEqual(
            permissions.allowed_pages(CASHIER), ("pos",)
        )
        self.assertEqual(permissions.CASHIER_PAGES, ("pos",))
        self.assertEqual(
            tuple(permissions.allowed_pages(MANAGER)), tuple(PAGES)
        )

    def test_default_page_lands_each_role_in_the_right_place(self) -> None:
        self.assertEqual(permissions.default_page(MANAGER), "dashboard")
        self.assertEqual(permissions.default_page(CASHIER), "pos")

    def test_cashier_has_exactly_two_capabilities(self) -> None:
        self.assertEqual(
            permissions.CASHIER_CAPABILITIES,
            frozenset({
                permissions.CAP_RECORD_SALE,
                permissions.CAP_PRINT_RECEIPT,
            }),
        )
        for capability in permissions.CASHIER_CAPABILITIES:
            self.assertTrue(permissions.can(CASHIER, capability))
        for capability in permissions.ALL_CAPABILITIES - permissions.CASHIER_CAPABILITIES:
            self.assertFalse(permissions.can(CASHIER, capability), capability)

    def test_manager_has_every_known_capability(self) -> None:
        for capability in permissions.ALL_CAPABILITIES:
            self.assertTrue(permissions.can(MANAGER, capability), capability)

    def test_unknown_capability_is_denied_even_for_a_manager(self) -> None:
        self.assertFalse(permissions.can(MANAGER, "delete_the_database"))
        self.assertFalse(permissions.can(CASHIER, "delete_the_database"))

    def test_unknown_role_falls_back_to_least_privilege(self) -> None:
        self.assertEqual(permissions.normalize_role("owner"), "cashier")
        self.assertEqual(permissions.normalize_role(""), "cashier")
        self.assertEqual(permissions.normalize_role(None), "cashier")
        self.assertEqual(permissions.role_of({"role": "admin"}), "cashier")
        self.assertEqual(permissions.allowed_pages({"role": "admin"}), ("pos",))

    def test_legacy_user_without_a_role_key_stays_a_manager(self) -> None:
        legacy = {"user_id": 1, "username": "admin"}
        self.assertEqual(permissions.role_of(legacy), "manager")
        self.assertEqual(
            permissions.allowed_pages(legacy), permissions.MANAGER_PAGES
        )

    def test_role_of_accepts_a_role_string_and_a_row_like_object(self) -> None:
        self.assertEqual(permissions.role_of("CASHIER"), "cashier")
        self.assertEqual(permissions.role_of(" Manager "), "manager")

        class Row:
            role = "cashier"

        self.assertEqual(permissions.role_of(Row()), "cashier")

    def test_role_label_is_human_readable(self) -> None:
        self.assertEqual(permissions.role_label(MANAGER), "Manager")
        self.assertEqual(permissions.role_label(CASHIER), "Cashier")
        self.assertEqual(permissions.role_label("cashier"), "Cashier")

    def test_session_set_current_and_clear(self) -> None:
        self.assertIsNone(permissions.current_session())
        permissions.set_session(CASHIER)
        self.assertIs(permissions.current_session(), CASHIER)
        permissions.clear_session()
        self.assertIsNone(permissions.current_session())

    def test_permission_denied_is_a_valueerror_with_a_friendly_message(self) -> None:
        self.assertTrue(issubclass(permissions.PermissionDenied, ValueError))
        with self.assertRaises(permissions.PermissionDenied) as caught:
            permissions.require(CASHIER, permissions.CAP_MANAGE_RECIPE)
        message = str(caught.exception)
        self.assertIn("not allowed to manage the recipe", message)
        self.assertIn("manager", message)

    def test_require_with_explicit_cashier_actor_denies_manager_capabilities(self) -> None:
        for capability in permissions.ALL_CAPABILITIES - permissions.CASHIER_CAPABILITIES:
            with self.subTest(capability=capability):
                with self.assertRaises(permissions.PermissionDenied):
                    permissions.require(CASHIER, capability)

    def test_require_allows_a_cashier_to_record_a_sale_and_print(self) -> None:
        permissions.require(CASHIER, permissions.CAP_RECORD_SALE)
        permissions.require(CASHIER, permissions.CAP_PRINT_RECEIPT)
        permissions.require(MANAGER, permissions.CAP_MANAGE_USERS)

    def test_require_falls_back_to_the_live_session_when_actor_is_omitted(self) -> None:
        permissions.set_session(CASHIER)
        with self.assertRaises(permissions.PermissionDenied):
            permissions.require(None, permissions.CAP_MANAGE_STOCK)

    def test_explicit_manager_actor_overrides_a_cashier_session(self) -> None:
        permissions.set_session(CASHIER)
        permissions.require(MANAGER, permissions.CAP_MANAGE_STOCK)

    def test_no_actor_and_no_session_allows_legacy_internal_callers(self) -> None:
        # Every Stage 1-9 test calls repositories this way; they must keep
        # working while the UI passes an explicit actor.
        for capability in permissions.ALL_CAPABILITIES:
            permissions.require(None, capability)


# ----------------------------------------------------------------------
# 2. Schema and safe v4 migration
# ----------------------------------------------------------------------
class SchemaAndMigrationTests(_TempDB):

    def test_schema_version_is_four(self) -> None:
        self.assertEqual(database.SCHEMA_VERSION, 4)
        self.assertEqual(
            self._query("PRAGMA user_version")[0][0], database.SCHEMA_VERSION
        )

    def test_fresh_users_table_has_role_default_and_check(self) -> None:
        schema = _source("database/schema.sql")
        self.assertIn("role                  TEXT    NOT NULL DEFAULT 'manager'", schema)
        self.assertIn("CHECK (role IN ('manager', 'cashier'))", schema)
        columns = {
            row["name"]: row
            for row in self._query("PRAGMA table_info(users)")
        }
        self.assertIn("role", columns)
        self.assertEqual(columns["role"]["dflt_value"], "'manager'")
        self.assertEqual(columns["role"]["notnull"], 1)

    def test_database_rejects_an_unknown_role(self) -> None:
        with closing(database.get_connection(self.db_path)) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO users "
                    "(username, password_hash, full_name, role, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    ("rogue", "hash", "Rogue", "owner",
                     database.local_timestamp()),
                )

    def test_seeded_administrator_is_a_manager(self) -> None:
        row = self._query("SELECT username, role, is_active FROM users")[0]
        self.assertEqual(row["username"], database.DEFAULT_ADMIN_USERNAME)
        self.assertEqual(row["role"], "manager")
        self.assertEqual(row["is_active"], 1)

    def _make_legacy(self) -> str:
        """Simulate the delivered pre-Stage-9 DB (no role, no unit_price)."""
        with closing(database.get_connection(self.db_path)) as conn:
            conn.execute("DROP TABLE sale_deductions")
            conn.execute("DROP TABLE sales_transactions")
            conn.execute("ALTER TABLE menu_items DROP COLUMN unit_price")
            conn.execute("ALTER TABLE users DROP COLUMN role")
            conn.execute("PRAGMA user_version = 1")
            conn.execute("UPDATE users SET password_hash = 'LEGACY-HASH'")
            conn.execute(
                "UPDATE ingredients SET quantity_on_hand = 777 "
                "WHERE name = 'Ramen broth'"
            )
            conn.commit()
        return "LEGACY-HASH"

    def test_legacy_database_gains_a_manager_role_without_data_loss(self) -> None:
        sentinel = self._make_legacy()
        database.initialize_database(self.db_path)
        columns = {
            row["name"] for row in self._query("PRAGMA table_info(users)")
        }
        self.assertIn("role", columns)
        self.assertIn("unit_price", {
            row["name"] for row in self._query("PRAGMA table_info(menu_items)")
        })
        rows = self._query(
            "SELECT username, role, password_hash FROM users"
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["role"], "manager")
        self.assertEqual(rows[0]["password_hash"], sentinel)
        self.assertEqual(
            self._query(
                "SELECT quantity_on_hand FROM ingredients "
                "WHERE name = 'Ramen broth'"
            )[0][0], 777,
        )
        self.assertEqual(len(self._query("SELECT * FROM ingredients")), 8)
        self.assertEqual(len(self._query("SELECT * FROM recipe_ingredients")), 8)
        self.assertEqual(
            self._query("PRAGMA user_version")[0][0], database.SCHEMA_VERSION
        )
        self.assertEqual(self._query("PRAGMA integrity_check")[0][0], "ok")
        self.assertEqual(self._query("PRAGMA foreign_key_check"), [])
        self.assertEqual(sales.get_unit_price(self.db_path), 150.0)

    def test_migration_never_reseeds_and_never_resets_a_password(self) -> None:
        with closing(database.get_connection(self.db_path)) as conn:
            conn.execute("UPDATE users SET password_hash = 'SENTINEL-HASH'")
            conn.execute("PRAGMA user_version = 1")
            conn.commit()
        database.initialize_database(self.db_path)
        self.assertEqual(len(self._query("SELECT * FROM users")), 1)
        self.assertEqual(
            self._query("SELECT password_hash FROM users")[0][0],
            "SENTINEL-HASH",
        )
        self.assertEqual(len(self._query("SELECT * FROM ingredients")), 8)
        self.assertEqual(len(self._query("SELECT * FROM menu_items")), 1)

    def test_migration_is_idempotent(self) -> None:
        sentinel = self._make_legacy()
        database.initialize_database(self.db_path)
        first = self._hash()
        database.initialize_database(self.db_path)
        database.initialize_database(self.db_path)
        self.assertEqual(self._hash(), first)
        self.assertEqual(
            self._query("SELECT password_hash FROM users")[0][0], sentinel
        )
        self.assertEqual(
            self._query("PRAGMA user_version")[0][0], database.SCHEMA_VERSION
        )

    def test_stage10_does_not_reseed_when_a_cashier_already_exists(self) -> None:
        self._create_cashier()
        before = len(self._query("SELECT * FROM users"))
        database.initialize_database(self.db_path)
        self.assertEqual(len(self._query("SELECT * FROM users")), before)
        self.assertEqual(
            self._query(
                "SELECT role FROM users WHERE username = 'counter'"
            )[0][0], "cashier",
        )

    def test_a_copy_of_the_real_database_gains_a_manager_role_safely(self) -> None:
        """The delivered DB may still be at v2. Prove the first Stage 10
        launch adds the role WITHOUT reseeding or resetting the admin's
        already-changed password. The original file is only ever read.
        """
        self.assertTrue(REAL_DB.exists())
        original_hash = hashlib.sha256(REAL_DB.read_bytes()).hexdigest()
        with closing(sqlite3.connect(
            f"{REAL_DB.as_uri()}?mode=ro", uri=True
        )) as conn:
            old_password_hash = conn.execute(
                "SELECT password_hash FROM users"
            ).fetchone()[0]
            old_user_count = conn.execute(
                "SELECT COUNT(*) FROM users"
            ).fetchone()[0]
            old_stock = conn.execute(
                "SELECT SUM(quantity_on_hand) FROM ingredients"
            ).fetchone()[0]

        with tempfile.TemporaryDirectory() as folder:
            copy = Path(folder) / "copy.db"
            shutil.copy2(REAL_DB, copy)
            database.initialize_database(copy)
            with closing(database.get_connection(copy)) as conn:
                columns = {
                    row["name"] for row in conn.execute(
                        "PRAGMA table_info(users)"
                    )
                }
                self.assertIn("role", columns)
                self.assertEqual(
                    conn.execute("PRAGMA user_version").fetchone()[0],
                    database.SCHEMA_VERSION,
                )
                self.assertEqual(
                    conn.execute("PRAGMA integrity_check").fetchone()[0], "ok"
                )
                self.assertEqual(
                    list(conn.execute("PRAGMA foreign_key_check")), []
                )
                users_now = conn.execute(
                    "SELECT username, role, password_hash FROM users"
                ).fetchall()
                self.assertEqual(len(users_now), old_user_count)
                self.assertEqual(users_now[0]["role"], "manager")
                self.assertEqual(
                    users_now[0]["password_hash"], old_password_hash
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT SUM(quantity_on_hand) FROM ingredients"
                    ).fetchone()[0], old_stock,
                )
        self.assertEqual(
            hashlib.sha256(REAL_DB.read_bytes()).hexdigest(), original_hash
        )

    def test_real_database_is_opened_read_only_and_stays_intact(self) -> None:
        self.assertTrue(REAL_DB.exists())
        before = hashlib.sha256(REAL_DB.read_bytes()).hexdigest()
        with closing(sqlite3.connect(
            f"{REAL_DB.as_uri()}?mode=ro", uri=True
        )) as conn:
            self.assertEqual(
                conn.execute("PRAGMA integrity_check").fetchone()[0], "ok"
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM menu_items").fetchone()[0], 1
            )
        self.assertEqual(
            hashlib.sha256(REAL_DB.read_bytes()).hexdigest(), before
        )


# ----------------------------------------------------------------------
# 3. Authentication
# ----------------------------------------------------------------------
class AuthenticationTests(_TempDB):

    def test_login_returns_the_role_and_never_the_hash(self) -> None:
        user = database.verify_credentials(
            database.DEFAULT_ADMIN_USERNAME,
            database.DEFAULT_ADMIN_PASSWORD,
            self.db_path,
        )
        self.assertIsNotNone(user)
        assert user is not None
        self.assertEqual(user["role"], "manager")
        self.assertNotIn("password_hash", user)
        self.assertEqual(user["must_change_password"], 1)

    def test_cashier_can_sign_in_and_is_forced_to_change_password(self) -> None:
        self._create_cashier()
        user = database.verify_credentials(
            "counter", TEMP_PASSWORD, self.db_path
        )
        self.assertIsNotNone(user)
        assert user is not None
        self.assertEqual(user["role"], "cashier")
        self.assertEqual(user["must_change_password"], 1)
        self.assertNotIn("password_hash", user)

    def test_a_cashier_lands_on_pos_not_the_dashboard(self) -> None:
        self._create_cashier()
        user = database.verify_credentials(
            "counter", TEMP_PASSWORD, self.db_path
        )
        self.assertEqual(permissions.default_page(user), "pos")
        self.assertEqual(permissions.allowed_pages(user), ("pos",))

    def test_deactivated_account_cannot_sign_in_and_can_be_reactivated(self) -> None:
        key = self._create_cashier()
        users.set_user_active(
            key, False, db_path=self.db_path, actor=MANAGER
        )
        self.assertIsNone(
            database.verify_credentials("counter", TEMP_PASSWORD, self.db_path)
        )
        users.set_user_active(key, True, db_path=self.db_path, actor=MANAGER)
        self.assertIsNotNone(
            database.verify_credentials("counter", TEMP_PASSWORD, self.db_path)
        )

    def test_default_password_hint_is_true_only_on_a_fresh_database(self) -> None:
        self.assertTrue(database.default_credentials_active(self.db_path))

    def test_pending_cashier_password_does_not_reveal_the_admin_password(self) -> None:
        """The Stage 10 fix: a pending cashier change must NOT re-show the
        admin's default credentials on the shared counter login screen."""
        database.change_password(
            1, database.DEFAULT_ADMIN_PASSWORD,
            NEW_ADMIN_PASSWORD, self.db_path,
        )
        self._create_cashier()
        self.assertEqual(
            self._query(
                "SELECT must_change_password FROM users "
                "WHERE username = 'counter'"
            )[0][0], 1,
        )
        self.assertFalse(database.default_credentials_active(self.db_path))


# ----------------------------------------------------------------------
# 4. User account management
# ----------------------------------------------------------------------
class UsersRepositoryTests(_TempDB):

    def test_create_user_hashes_password_and_forces_a_change(self) -> None:
        key = self._create_cashier()
        row = self._query(
            "SELECT * FROM users WHERE user_id = ?", (key,)
        )[0]
        self.assertNotEqual(row["password_hash"], TEMP_PASSWORD)
        self.assertTrue(row["password_hash"].startswith("pbkdf2_sha256$"))
        self.assertTrue(
            database.verify_password(TEMP_PASSWORD, row["password_hash"])
        )
        self.assertEqual(row["role"], "cashier")
        self.assertEqual(row["is_active"], 1)
        self.assertEqual(row["must_change_password"], 1)

    def test_list_users_never_exposes_a_password_hash(self) -> None:
        self._create_cashier()
        rows = users.list_users(self.db_path, actor=MANAGER)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["role"], "manager")   # managers first
        for row in rows:
            self.assertNotIn("password_hash", row)
        self.assertNotIn("password_hash", inspect.getsource(users.list_users))

    def test_summary_counts_roles_and_active_accounts(self) -> None:
        self._create_cashier()
        counts = users.summary(self.db_path, actor=MANAGER)
        self.assertEqual(
            counts,
            {"total": 2, "active": 2, "managers": 1, "cashiers": 1},
        )

    def test_create_user_rejects_a_duplicate_username(self) -> None:
        self._create_cashier()
        with self.assertRaisesRegex(ValueError, "already taken"):
            users.create_user(
                "COUNTER", TEMP_PASSWORD, "Second",
                permissions.ROLE_CASHIER,
                db_path=self.db_path, actor=MANAGER,
            )

    def test_create_user_validates_username_and_full_name(self) -> None:
        bad_values = ("", "   ", "has space", "x" * 41)
        for value in bad_values:
            with self.subTest(username=value):
                with self.assertRaises(ValueError):
                    users.create_user(
                        value, TEMP_PASSWORD, "Name",
                        permissions.ROLE_CASHIER,
                        db_path=self.db_path, actor=MANAGER,
                    )
        with self.assertRaises(ValueError):
            users.create_user(
                "valid", TEMP_PASSWORD, "",
                permissions.ROLE_CASHIER,
                db_path=self.db_path, actor=MANAGER,
            )

    def test_create_user_validates_password_strength_and_role(self) -> None:
        with self.assertRaises(ValueError):
            users.create_user(
                "weak", "short", "Name", permissions.ROLE_CASHIER,
                db_path=self.db_path, actor=MANAGER,
            )
        with self.assertRaises(ValueError):
            users.create_user(
                "badrole", TEMP_PASSWORD, "Name", "owner",
                db_path=self.db_path, actor=MANAGER,
            )
        self.assertEqual(len(self._query("SELECT * FROM users")), 1)

    def test_cashier_cannot_create_an_account_or_reset_a_password(self) -> None:
        before = self._hash()
        with self.assertRaises(permissions.PermissionDenied):
            users.create_user(
                "sneaky", TEMP_PASSWORD, "Sneaky",
                permissions.ROLE_MANAGER,
                db_path=self.db_path, actor=CASHIER,
            )
        with self.assertRaises(permissions.PermissionDenied):
            users.reset_password(
                1, TEMP_PASSWORD, db_path=self.db_path, actor=CASHIER
            )
        self.assertEqual(self._hash(), before)
        self.assertEqual(len(self._query("SELECT * FROM users")), 1)

    def test_cashier_cannot_even_read_the_account_list_or_summary(self) -> None:
        with self.assertRaises(permissions.PermissionDenied):
            users.list_users(self.db_path, actor=CASHIER)
        with self.assertRaises(permissions.PermissionDenied):
            users.summary(self.db_path, actor=CASHIER)

    def test_cashier_cannot_deactivate_an_account(self) -> None:
        key = self._create_cashier()
        with self.assertRaises(permissions.PermissionDenied):
            users.set_user_active(
                key, False, db_path=self.db_path, actor=CASHIER
            )
        self.assertEqual(
            self._query(
                "SELECT is_active FROM users WHERE user_id = ?", (key,)
            )[0][0], 1,
        )

    def test_manager_cannot_deactivate_their_own_account(self) -> None:
        with self.assertRaisesRegex(ValueError, "your own account"):
            users.set_user_active(
                1, False, db_path=self.db_path, actor=MANAGER
            )
        self.assertEqual(
            self._query("SELECT is_active FROM users WHERE user_id = 1")[0][0], 1
        )

    def test_last_active_manager_cannot_be_deactivated(self) -> None:
        self.assertEqual(users.count_active_managers(self.db_path), 1)
        with self.assertRaisesRegex(ValueError, "At least one active manager"):
            users.set_user_active(
                1, False, db_path=self.db_path, actor=OTHER_MANAGER
            )

    def test_a_second_manager_allows_deactivating_the_first(self) -> None:
        users.create_user(
            "owner", TEMP_PASSWORD, "Owner",
            permissions.ROLE_MANAGER,
            db_path=self.db_path, actor=MANAGER,
        )
        self.assertEqual(users.count_active_managers(self.db_path), 2)
        users.set_user_active(
            1, False, db_path=self.db_path, actor=OTHER_MANAGER
        )
        self.assertEqual(users.count_active_managers(self.db_path), 1)

    def test_deactivation_is_a_status_change_not_a_delete(self) -> None:
        key = self._create_cashier()
        before = self._query("SELECT * FROM users WHERE user_id = ?", (key,))[0]
        users.set_user_active(
            key, False, db_path=self.db_path, actor=MANAGER
        )
        after = self._query("SELECT * FROM users WHERE user_id = ?", (key,))[0]
        self.assertEqual(len(self._query("SELECT * FROM users")), 2)
        self.assertEqual(after["is_active"], 0)
        self.assertEqual(after["password_hash"], before["password_hash"])

    def test_reset_password_changes_the_hash_and_forces_a_change(self) -> None:
        key = self._create_cashier()
        old = self._query(
            "SELECT password_hash FROM users WHERE user_id = ?", (key,)
        )[0][0]
        users.reset_password(
            key, "Another2026!", db_path=self.db_path, actor=MANAGER
        )
        new = self._query(
            "SELECT password_hash, must_change_password "
            "FROM users WHERE user_id = ?", (key,)
        )[0]
        self.assertNotEqual(new["password_hash"], old)
        self.assertNotEqual(new["password_hash"], "Another2026!")
        self.assertEqual(new["must_change_password"], 1)
        self.assertTrue(
            database.verify_password("Another2026!", new["password_hash"])
        )

    def test_invalid_user_ids_are_rejected(self) -> None:
        for bad in (None, "abc", "", 0.5):
            with self.subTest(user_id=bad):
                with self.assertRaises(ValueError):
                    users.set_user_active(
                        bad, False, db_path=self.db_path, actor=MANAGER
                    )
        with self.assertRaisesRegex(ValueError, "not found"):
            users.set_user_active(
                9999, False, db_path=self.db_path, actor=MANAGER
            )
        with self.assertRaisesRegex(ValueError, "not found"):
            users.reset_password(
                9999, TEMP_PASSWORD, db_path=self.db_path, actor=MANAGER
            )

    def test_a_live_cashier_session_blocks_account_writes_without_an_actor(self) -> None:
        """Defence in depth: omitting `actor` is NOT a bypass."""
        permissions.set_session(CASHIER)
        before = self._hash()
        with self.assertRaises(permissions.PermissionDenied):
            users.create_user(
                "sneaky", TEMP_PASSWORD, "Sneaky",
                permissions.ROLE_CASHIER, db_path=self.db_path,
            )
        with self.assertRaises(permissions.PermissionDenied):
            users.list_users(self.db_path)
        self.assertEqual(self._hash(), before)

    def test_session_fallback_still_blocks_self_deactivation(self) -> None:
        permissions.set_session(MANAGER)
        with self.assertRaisesRegex(ValueError, "your own account"):
            users.set_user_active(1, False, db_path=self.db_path)


# ----------------------------------------------------------------------
# 5. Repository guards across every other write path
# ----------------------------------------------------------------------
class RepositoryGuardTests(_TempDB):

    def test_a_cashier_session_blocks_every_manager_write(self) -> None:
        permissions.set_session(CASHIER)
        before = self._hash()
        attempts: list[tuple[str, Any]] = [
            ("add_ingredient",
             lambda: repository.add_ingredient(
                 "Sneaky", "grams", 1, "", db_path=self.db_path)),
            ("delete_ingredient",
             lambda: repository.delete_ingredient(1, db_path=self.db_path)),
            ("recipe.update_quantity",
             lambda: recipe.update_quantity(1, 2, db_path=self.db_path)),
            ("recipe.update_notes",
             lambda: recipe.update_notes("x", db_path=self.db_path)),
            ("stock.record_stock_in",
             lambda: stock.record_stock_in([], db_path=self.db_path)),
            ("stock.record_stock_out",
             lambda: stock.record_stock_out([], "Consumption",
                                            db_path=self.db_path)),
            ("sales.set_unit_price",
             lambda: sales.set_unit_price(200, db_path=self.db_path)),
            ("sales.void_sale",
             lambda: sales.void_sale(1, "reason", db_path=self.db_path)),
            ("users.create_user",
             lambda: users.create_user(
                 "sneaky", TEMP_PASSWORD, "Sneaky",
                 permissions.ROLE_CASHIER, db_path=self.db_path)),
        ]
        for name, call in attempts:
            with self.subTest(write=name):
                with self.assertRaises(permissions.PermissionDenied):
                    call()
        self.assertEqual(self._hash(), before, "a denied write touched the DB")

    def test_cashier_may_still_record_a_sale_and_print_a_receipt(self) -> None:
        self.assertTrue(
            permissions.can(CASHIER, permissions.CAP_RECORD_SALE)
        )
        self.assertTrue(
            permissions.can(CASHIER, permissions.CAP_PRINT_RECEIPT)
        )
        permissions.set_session(CASHIER)
        permissions.require(None, permissions.CAP_RECORD_SALE)
        permissions.require(None, permissions.CAP_PRINT_RECEIPT)

    def test_explicit_manager_actor_overrides_a_cashier_session(self) -> None:
        permissions.set_session(CASHIER)
        key = repository.add_ingredient(
            "Test Herb", "grams", 1, "",
            db_path=self.db_path, actor=MANAGER,
        )
        self.assertGreater(key, 0)
        self.assertEqual(
            sales.set_unit_price(175.0, db_path=self.db_path, actor=MANAGER),
            175.0,
        )
        self.assertEqual(
            recipe.update_notes(
                "Stage 10 note", db_path=self.db_path, actor=MANAGER
            ),
            "Stage 10 note",
        )

    def test_legacy_internal_calls_without_a_session_still_work(self) -> None:
        key = repository.add_ingredient(
            "Legacy Herb", "grams", 1, "", db_path=self.db_path
        )
        self.assertGreater(key, 0)
        self.assertEqual(
            sales.set_unit_price(160.0, db_path=self.db_path), 160.0
        )

    def test_every_write_function_declares_an_actor(self) -> None:
        pairs = (
            (repository.add_ingredient, "actor"),
            (repository.update_ingredient, "actor"),
            (repository.delete_ingredient, "actor"),
            (recipe.update_quantity, "actor"),
            (recipe.add_ingredient, "actor"),
            (recipe.remove_ingredient, "actor"),
            (recipe.update_notes, "actor"),
            (stock.record_stock_in, "actor"),
            (stock.record_stock_out, "actor"),
            (sales.set_unit_price, "actor"),
            (sales.confirm_sale, "actor"),
            (sales.void_sale, "actor"),
            (users.create_user, "actor"),
            (users.set_user_active, "actor"),
            (users.reset_password, "actor"),
        )
        for function, parameter in pairs:
            with self.subTest(function=function.__name__):
                self.assertIn(
                    parameter, inspect.signature(function).parameters
                )
                self.assertIn(
                    "permissions.require",
                    inspect.getsource(function),
                )


# ----------------------------------------------------------------------
# 6. Shell / UI wiring (source-level, no Tk window opened)
# ----------------------------------------------------------------------
class UiWiringTests(_SessionIsolation):

    def test_shell_registers_ten_pages_including_menu_and_users(self) -> None:
        self.assertEqual(
            list(PAGES),
            [
                "dashboard", "ingredients", "products", "menu", "recipe",
                "calculator", "pos", "reports", "inventory", "history",
                "monitoring", "users",
            ],
        )
        self.assertEqual(PAGES["users"]["label"], "Users")
        self.assertTrue(callable(PAGES["users"]["view"].refresh))

    def test_shell_filters_sidebar_and_refuses_forbidden_navigation(self) -> None:
        source = _source("components/shell.py")
        self.assertIn("permissions.allowed_pages(user)", source)
        self.assertIn("if key in self.allowed_keys", source)
        self.assertIn("key not in self.allowed_keys", source)
        self.assertIn("nav_items", source)
        self.assertIn("permissions.set_session(user)", source)
        self.assertIn("permissions.default_page(user)", source)

    def test_main_clears_the_session_on_logout_and_restores_it(self) -> None:
        source = _source("main.py")
        self.assertIn("permissions.clear_session()", source)
        self.assertIn("permissions.set_session(user)", source)
        self.assertIn("12 pages", source)

    def test_header_shows_the_signed_in_role(self) -> None:
        source = _source("components/header.py")
        self.assertIn("permissions.role_label(user)", source)

    def test_users_page_has_a_denied_state_and_guards_its_reads(self) -> None:
        source = _source("pages/users.py")
        self.assertIn("_build_denied", source)
        self.assertIn("self.allowed", source)
        self.assertIn("users.list_users(actor=self.actor)", source)
        self.assertIn("users.summary(actor=self.actor)", source)
        self.assertIn("actor=self.actor", source)

    def test_pos_price_and_void_controls_are_manager_only(self) -> None:
        source = _source("pages/pos.py")
        self.assertIn(
            "if permissions.can(self.user, permissions.CAP_SET_PRICE):", source
        )
        self.assertIn(
            "if not permissions.can(self.user, permissions.CAP_VOID_SALE):",
            source,
        )
        self.assertIn("permissions.is_manager(self.user)", source)

    def test_pos_passes_the_real_session_actor_into_every_write(self) -> None:
        source = _source("pages/pos.py")
        self.assertIn("actor=self.actor", source)       # PriceDialog
        self.assertIn("actor=self.user", source)        # confirm / void
        self.assertIn("sales.confirm_sale(", source)
        self.assertIn("sales.void_sale(", source)

    def test_exact_total_recomputes_from_live_price_and_servings(self) -> None:
        """Stage 9's stale-preview bug must stay fixed."""
        from pages.pos import PosView

        source = inspect.getsource(PosView._use_exact_total)
        self.assertIn("sales.get_unit_price(", source)
        self.assertIn("self.servings_entry.get()", source)
        self.assertIn("format_number(total)", source)
        # The old stale-preview expression must be gone from EXECUTABLE code.
        # The docstring legitimately mentions it, so inspection is AST-based.
        tree = ast.parse(source.strip())
        attributes = {
            ast.unparse(node)
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
        }
        self.assertNotIn("self.preview.total_amount", attributes)
        self.assertIn("self.tendered_entry.insert", attributes)

    def test_cashier_summary_hides_turnover_figures(self) -> None:
        from pages.pos import PosView

        source = inspect.getsource(PosView._render_summary)
        self.assertIn("permissions.is_manager(self.user)", source)
        self.assertIn("Gross sales", source)   # still shown to managers

    def test_confirm_button_visibly_looks_disabled(self) -> None:
        """A disabled CTkButton keeps its colour, so state alone was not enough
        on the shared counter PC. All state changes must go through the helper.
        """
        from pages.pos import PosView

        source = inspect.getsource(PosView._set_confirm_enabled)
        self.assertIn('state="normal" if enabled else "disabled"', source)
        self.assertIn("fg_color=colour", source)
        self.assertIn("SUCCESS if enabled else CONFIRM_DISABLED_COLOR", source)
        module = _source("pages/pos.py")
        self.assertIn("CONFIRM_DISABLED_COLOR", module)
        self.assertNotIn(
            'self.confirm_button.configure(state="disabled")', module
        )
        self.assertNotIn(
            'self.confirm_button.configure(state="normal")', module
        )
        # Stage 11b's dish picker added one more legitimate disable call
        # (changing dish invalidates a stale preview).
        self.assertEqual(module.count("self._set_confirm_enabled(False)"), 5)
        self.assertEqual(module.count("self._set_confirm_enabled(True)"), 1)

    def test_money_is_still_philippine_peso(self) -> None:
        self.assertEqual(formatting.CURRENCY_CODE, "PHP")
        self.assertEqual(formatting.CURRENCY_SYMBOL, "\u20b1")
        self.assertEqual(formatting.format_money(150), "\u20b1150.00")
        for relative in (
            "formatting.py", "receipt.py", "pages/pos.py",
            "database/schema.sql",
        ):
            with self.subTest(file=relative):
                text = _source(relative)
                self.assertNotIn("\u00a5", text)
                self.assertNotIn("\uffe5", text)

    def test_stage11a_schema_allows_many_dishes_but_one_primary(self) -> None:
        # Stage 11a deliberately replaces the "one dish EVER" rule with
        # "many dishes, exactly one primary POS dish". Regression baseline
        # updated to match the approved Stage 11a behaviour.
        schema = _source("database/schema.sql")
        self.assertIn("singleton", schema)
        self.assertIn("CHECK (singleton IN (0, 1))", schema)
        self.assertIn("ux_menu_items_primary", schema)
        self.assertIn("WHERE singleton = 1", schema)
        self.assertNotIn("ux_menu_items_single", schema)
        self.assertNotIn("CHECK (singleton = 1)", schema)
        self.assertEqual(
            schema.count("CREATE TABLE IF NOT EXISTS menu_items"), 1
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
