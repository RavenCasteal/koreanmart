"""
Stage 11a automated checks - multiple dishes, one primary POS dish.

Run from the project folder:
    .venv\\Scripts\\python.exe -m unittest discover -s tests -v

What these tests protect:
  * the v4 schema allows many dishes but EXACTLY ONE primary (POS) dish
  * the SQLite table rebuild migration is atomic, idempotent and never loses
    the admin's already-changed password, recipe links or stock balances
  * each dish owns its own recipe; editing dish B can never touch dish A
  * Calculator and POS still read ONLY the primary dish's recipe, so a second
    dish can never over-deduct shared stock in Stage 11a
  * dish management is manager-only (cashiers still see just POS)
  * prices stay Philippine peso (PHP); no yen symbol anywhere
  * the delivered real database is never written by any test here
"""

from __future__ import annotations

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
import menu_repository as dishes
import permissions
import recipe_repository as recipes
import sales_repository as sales
import usage_repository as usage
from components.shell import PAGES
from formatting import CURRENCY_SYMBOL, format_money

ROOT = Path(__file__).resolve().parent.parent
REAL_DB = ROOT / "database" / "ramen_inventory.db"
MANAGER: dict[str, Any] = {"user_id": 1, "role": "manager", "username": "admin"}
CASHIER: dict[str, Any] = {"user_id": 2, "role": "cashier", "username": "counter"}


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


# A deliberately OLD (Stage 10 / user_version 3) database definition.
# SQLite cannot DROP a CHECK, so this is the exact situation the migration
# must repair. Full schema.sql is NOT used here on purpose.
LEGACY_V3_SQL = """
CREATE TABLE users (
    user_id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    full_name TEXT NOT NULL DEFAULT 'Administrator',
    role TEXT NOT NULL DEFAULT 'manager'
          CHECK (role IN ('manager', 'cashier')),
    is_active INTEGER NOT NULL DEFAULT 1,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_login_at TEXT
);
CREATE TABLE menu_items (
    menu_item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    singleton INTEGER NOT NULL DEFAULT 1 CHECK (singleton = 1),
    unit_price REAL NOT NULL DEFAULT 150.0,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_menu_items_single ON menu_items (singleton);
CREATE TABLE recipes (
    recipe_id INTEGER PRIMARY KEY AUTOINCREMENT,
    menu_item_id INTEGER NOT NULL UNIQUE
        REFERENCES menu_items (menu_item_id) ON DELETE RESTRICT,
    notes TEXT,
    updated_at TEXT
);
CREATE TABLE ingredients (
    ingredient_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    unit TEXT NOT NULL,
    quantity_on_hand REAL NOT NULL DEFAULT 0,
    minimum_stock REAL NOT NULL DEFAULT 0,
    expiration_date TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE recipe_ingredients (
    recipe_ingredient_id INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe_id INTEGER NOT NULL
        REFERENCES recipes (recipe_id) ON DELETE CASCADE,
    ingredient_id INTEGER NOT NULL
        REFERENCES ingredients (ingredient_id) ON DELETE RESTRICT,
    quantity_per_serving REAL NOT NULL,
    UNIQUE (recipe_id, ingredient_id)
);
INSERT INTO users (username, password_hash, full_name, role, created_at)
VALUES ('admin', 'SENTINEL-HASH', 'Administrator', 'manager',
        '2026-01-01 00:00:00');
INSERT INTO menu_items (name, description, created_at)
VALUES ('Regular Ramen', 'legacy description', '2026-01-01 00:00:00');
INSERT INTO recipes (menu_item_id, notes, updated_at)
VALUES (1, 'legacy notes', '2026-01-01 00:00:00');
INSERT INTO ingredients
    (name, unit, quantity_on_hand, minimum_stock, created_at, updated_at)
VALUES ('Ramen noodles', 'grams', 777, 300,
        '2026-01-01 00:00:00', '2026-01-01 00:00:00');
INSERT INTO recipe_ingredients (recipe_id, ingredient_id, quantity_per_serving)
VALUES (1, 1, 150);
PRAGMA user_version = 3;
"""


class _TempDB(unittest.TestCase):
    """Every test gets a fresh, isolated SQLite file (never the real one)."""

    def setUp(self) -> None:
        self._folder = tempfile.TemporaryDirectory()
        self.db_path = Path(self._folder.name) / "test.db"
        permissions.clear_session()
        database.initialize_database(self.db_path)

    def tearDown(self) -> None:
        permissions.clear_session()
        self._folder.cleanup()

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with closing(database.get_connection(self.db_path)) as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def _scalar(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        rows = self._query(sql, params)
        return rows[0][list(rows[0])[0]] if rows else None

    def _add_dish(self, name: str = "Spicy Ramen", price: str = "180.00") -> int:
        return dishes.add_dish(
            name, "A second ramen variant", price,
            db_path=self.db_path, actor=MANAGER,
        )

    def _stock_up(self, multiplier: float = 10.0) -> None:
        """Give the PRIMARY recipe enough stock for a real sale."""
        with database.transaction(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT ri.ingredient_id, ri.quantity_per_serving
                FROM recipe_ingredients AS ri
                JOIN recipes AS r ON r.recipe_id = ri.recipe_id
                JOIN menu_items AS m ON m.menu_item_id = r.menu_item_id
                WHERE m.singleton = 1
                """
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE ingredients SET quantity_on_hand = ? "
                    "WHERE ingredient_id = ?",
                    (float(row["quantity_per_serving"]) * multiplier,
                     int(row["ingredient_id"])),
                )


# ----------------------------------------------------------------------
# 1. Fresh v4 schema
# ----------------------------------------------------------------------
class FreshSchemaTests(_TempDB):

    def test_schema_version_is_four(self) -> None:
        self.assertEqual(database.SCHEMA_VERSION, 4)
        self.assertEqual(
            self._query("PRAGMA user_version")[0]["user_version"], 4
        )

    def test_seed_still_creates_exactly_one_primary_dish(self) -> None:
        rows = self._query("SELECT * FROM menu_items")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Regular Ramen")
        self.assertEqual(rows[0]["singleton"], 1)
        self.assertEqual(rows[0]["unit_price"], 150.0)

    def test_fresh_table_has_no_legacy_check(self) -> None:
        ddl = self._query(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'menu_items'"
        )[0]["sql"]
        self.assertNotIn("CHECK (singleton = 1)", ddl)
        self.assertIn("CHECK (singleton IN (0, 1))", ddl)

    def test_partial_unique_index_allows_many_dishes_but_one_primary(self) -> None:
        names = [row["name"] for row in self._query(
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND tbl_name = 'menu_items'"
        )]
        self.assertIn("ux_menu_items_primary", names)
        self.assertNotIn("ux_menu_items_single", names)
        with database.transaction(self.db_path) as conn:
            conn.execute(
                "INSERT INTO menu_items (name, singleton, created_at) "
                "VALUES ('Extra A', 0, ?)",
                (database.local_timestamp(),),
            )
            conn.execute(
                "INSERT INTO menu_items (name, singleton, created_at) "
                "VALUES ('Extra B', 0, ?)",
                (database.local_timestamp(),),
            )
        self._query("SELECT * FROM menu_items")
        with self.assertRaises(sqlite3.IntegrityError):
            with database.transaction(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO menu_items (name, singleton, created_at) "
                    "VALUES ('Second primary', 1, ?)",
                    (database.local_timestamp(),),
                )

    def test_autoincrement_sequence_survives_for_new_dishes(self) -> None:
        first = self._add_dish()
        second = self._add_dish("Miso Ramen", "170")
        self.assertGreater(first, 1)
        self.assertEqual(second, first + 1)


# ----------------------------------------------------------------------
# 2. Legacy v3 -> v4 migration (atomic, idempotent, non-destructive)
# ----------------------------------------------------------------------
class MigrationTests(unittest.TestCase):

    def _legacy_db(self, folder: Path) -> Path:
        path = folder / "legacy.db"
        with closing(sqlite3.connect(str(path))) as conn:
            conn.executescript(LEGACY_V3_SQL)
            conn.commit()
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0], 3
            )
        return path

    def test_legacy_database_rebuilds_without_losing_anything(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = self._legacy_db(Path(name))
            database.initialize_database(path)
            with closing(database.get_connection(path)) as conn:
                self.assertEqual(
                    conn.execute("PRAGMA user_version").fetchone()[0], 4
                )
                self.assertEqual(
                    conn.execute("PRAGMA integrity_check").fetchone()[0], "ok"
                )
                self.assertEqual(
                    list(conn.execute("PRAGMA foreign_key_check")), []
                )
                ddl = conn.execute(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type='table' AND name='menu_items'"
                ).fetchone()[0]
                self.assertNotIn("CHECK (singleton = 1)", ddl)
                self.assertIn("CHECK (singleton IN (0, 1))", ddl)
                indexes = {
                    row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type='index' AND tbl_name='menu_items'"
                    )
                }
                self.assertIn("ux_menu_items_primary", indexes)
                self.assertNotIn("ux_menu_items_single", indexes)
                menu = conn.execute(
                    "SELECT * FROM menu_items"
                ).fetchall()
                self.assertEqual(len(menu), 1)
                self.assertEqual(menu[0]["menu_item_id"], 1)
                self.assertEqual(menu[0]["name"], "Regular Ramen")
                self.assertEqual(menu[0]["singleton"], 1)
                self.assertEqual(menu[0]["unit_price"], 150.0)
                self.assertEqual(menu[0]["description"], "legacy description")
                self.assertEqual(
                    conn.execute(
                        "SELECT password_hash FROM users"
                    ).fetchone()[0], "SENTINEL-HASH"
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT quantity_on_hand FROM ingredients"
                    ).fetchone()[0], 777
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT notes FROM recipes"
                    ).fetchone()[0], "legacy notes"
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM recipe_ingredients"
                    ).fetchone()[0], 1
                )

    def test_migration_is_idempotent_and_does_not_rewrite_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = self._legacy_db(Path(name))
            database.initialize_database(path)
            first = hashlib.sha256(path.read_bytes()).hexdigest()
            database.initialize_database(path)
            database.initialize_database(path)
            self.assertEqual(
                hashlib.sha256(path.read_bytes()).hexdigest(), first
            )

    def test_migrated_database_accepts_a_second_dish(self) -> None:
        permissions.clear_session()
        with tempfile.TemporaryDirectory() as name:
            path = self._legacy_db(Path(name))
            database.initialize_database(path)
            new_id = dishes.add_dish(
                "Spicy Ramen", "Extra chilli", "180.00",
                db_path=path, actor=MANAGER,
            )
            with closing(database.get_connection(path)) as conn:
                rows = conn.execute(
                    "SELECT name, singleton FROM menu_items "
                    "ORDER BY menu_item_id"
                ).fetchall()
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[1]["singleton"], 0)
            self.assertGreater(new_id, 1)

    def test_a_copy_of_the_real_database_migrates_and_the_original_is_untouched(
        self,
    ) -> None:
        if not REAL_DB.exists():
            self.skipTest("Delivered database is not present.")
        before = hashlib.sha256(REAL_DB.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory() as name:
            copy = Path(name) / "copy.db"
            shutil.copy2(REAL_DB, copy)
            with closing(database.get_connection(copy)) as conn:
                users_before = conn.execute(
                    "SELECT COUNT(*) FROM users"
                ).fetchone()[0]
                hashes_before = [
                    row[0] for row in conn.execute(
                        "SELECT password_hash FROM users ORDER BY user_id"
                    )
                ]
                stock_before = [
                    tuple(row) for row in conn.execute(
                        "SELECT ingredient_id, quantity_on_hand "
                        "FROM ingredients ORDER BY ingredient_id"
                    )
                ]
            database.initialize_database(copy)
            with closing(database.get_connection(copy)) as conn:
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
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
                    users_before,
                )
                self.assertEqual(
                    [row[0] for row in conn.execute(
                        "SELECT password_hash FROM users ORDER BY user_id"
                    )],
                    hashes_before,
                )
                self.assertEqual(
                    [tuple(row) for row in conn.execute(
                        "SELECT ingredient_id, quantity_on_hand "
                        "FROM ingredients ORDER BY ingredient_id"
                    )],
                    stock_before,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM menu_items WHERE singleton = 1"
                    ).fetchone()[0], 1
                )
        self.assertEqual(
            hashlib.sha256(REAL_DB.read_bytes()).hexdigest(), before
        )


# ----------------------------------------------------------------------
# 3. Menu repository validation and CRUD
# ----------------------------------------------------------------------
class MenuRepositoryTests(_TempDB):

    def test_add_dish_creates_a_non_primary_dish_and_its_own_recipe(self) -> None:
        key = self._add_dish()
        row = self._query(
            "SELECT * FROM menu_items WHERE menu_item_id = ?", (key,)
        )[0]
        self.assertEqual(row["name"], "Spicy Ramen")
        self.assertEqual(row["description"], "A second ramen variant")
        self.assertEqual(row["unit_price"], 180.0)
        self.assertEqual(row["singleton"], 0)
        self.assertEqual(row["is_active"], 1)
        recipe = self._query(
            "SELECT * FROM recipes WHERE menu_item_id = ?", (key,)
        )
        self.assertEqual(len(recipe), 1)
        self.assertEqual(
            self._scalar(
                "SELECT COUNT(*) FROM recipe_ingredients WHERE recipe_id = ?",
                (recipe[0]["recipe_id"],),
            ), 0
        )

    def test_add_dish_never_changes_stock_or_creates_movements(self) -> None:
        before = self._query(
            "SELECT ingredient_id, quantity_on_hand FROM ingredients "
            "ORDER BY ingredient_id"
        )
        self._add_dish()
        self.assertEqual(
            self._query(
                "SELECT ingredient_id, quantity_on_hand FROM ingredients "
                "ORDER BY ingredient_id"
            ), before
        )
        self.assertEqual(
            self._scalar("SELECT COUNT(*) FROM vw_stock_movements"), 0
        )

    def test_duplicate_dish_name_is_rejected_case_insensitively(self) -> None:
        self._add_dish()
        with self.assertRaisesRegex(ValueError, "already exists"):
            dishes.add_dish(
                "spicy RAMEN", "", "190", db_path=self.db_path, actor=MANAGER
            )

    def test_name_validation(self) -> None:
        for bad in ("", "   ", "x" * 61):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    dishes.validate_name(bad)

    def test_description_validation(self) -> None:
        self.assertEqual(dishes.validate_description(None), "")
        with self.assertRaises(ValueError):
            dishes.validate_description("x" * 201)

    def test_price_validation_rejects_zero_negative_and_junk(self) -> None:
        for bad in ("", "abc", "0", "-10", "nan", "inf", "1e309"):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    dishes.validate_unit_price(bad)
        self.assertEqual(dishes.validate_unit_price("\u20b11,250.505"), 1250.51)

    def test_update_dish_changes_price_without_touching_the_primary(self) -> None:
        key = self._add_dish()
        dishes.update_dish(
            key, "Spicy Ramen Deluxe", "Now with extra chilli", "199.50",
            db_path=self.db_path, actor=MANAGER,
        )
        row = self._query(
            "SELECT * FROM menu_items WHERE menu_item_id = ?", (key,)
        )[0]
        self.assertEqual(row["name"], "Spicy Ramen Deluxe")
        self.assertEqual(row["unit_price"], 199.5)
        self.assertEqual(
            self._scalar(
                "SELECT unit_price FROM menu_items WHERE singleton = 1"
            ), 150.0
        )

    def test_primary_dish_name_cannot_be_renamed_in_stage_11a(self) -> None:
        primary = self._query(
            "SELECT * FROM menu_items WHERE singleton = 1"
        )[0]
        with self.assertRaisesRegex(ValueError, "keeps its name"):
            dishes.update_dish(
                primary["menu_item_id"], "Renamed Ramen",
                primary["description"], "150",
                db_path=self.db_path, actor=MANAGER,
            )

    def test_primary_dish_price_and_description_are_still_editable(self) -> None:
        primary = self._query(
            "SELECT * FROM menu_items WHERE singleton = 1"
        )[0]
        dishes.update_dish(
            primary["menu_item_id"], "Regular Ramen", "House special", "165",
            db_path=self.db_path, actor=MANAGER,
        )
        row = self._query(
            "SELECT * FROM menu_items WHERE menu_item_id = ?",
            (primary["menu_item_id"],),
        )[0]
        self.assertEqual(row["unit_price"], 165.0)
        self.assertEqual(row["description"], "House special")

    def test_update_dish_rejects_a_name_used_by_another_dish(self) -> None:
        key = self._add_dish()
        with self.assertRaisesRegex(ValueError, "already exists"):
            dishes.update_dish(
                key, "Regular Ramen", "", "180",
                db_path=self.db_path, actor=MANAGER,
            )

    def test_deactivation_is_a_status_change_not_a_delete(self) -> None:
        key = self._add_dish()
        dishes.set_active(key, False, db_path=self.db_path, actor=MANAGER)
        rows = self._query(
            "SELECT * FROM menu_items WHERE menu_item_id = ?", (key,)
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["is_active"], 0)
        self.assertEqual(
            len(self._query(
                "SELECT * FROM recipes WHERE menu_item_id = ?", (key,)
            )), 1
        )
        dishes.set_active(key, True, db_path=self.db_path, actor=MANAGER)
        self.assertEqual(
            self._scalar(
                "SELECT is_active FROM menu_items WHERE menu_item_id = ?",
                (key,),
            ), 1
        )

    def test_primary_dish_cannot_be_deactivated(self) -> None:
        primary = self._query(
            "SELECT menu_item_id FROM menu_items WHERE singleton = 1"
        )[0]
        with self.assertRaisesRegex(ValueError, "cannot be deactivated"):
            dishes.set_active(
                primary["menu_item_id"], False,
                db_path=self.db_path, actor=MANAGER,
            )

    def test_get_dish_and_summary_counts_are_live(self) -> None:
        key = self._add_dish()
        self.assertEqual(
            dishes.get_dish(key, db_path=self.db_path, actor=MANAGER)["name"],
            "Spicy Ramen",
        )
        with self.assertRaises(ValueError):
            dishes.get_dish(99999, db_path=self.db_path, actor=MANAGER)
        counts = dishes.summary(db_path=self.db_path, actor=MANAGER)
        self.assertEqual(counts["total"], 2)
        self.assertEqual(counts["active"], 2)
        self.assertEqual(counts["primary_count"], 1)
        self.assertEqual(counts["with_recipe"], 1)

    def test_list_dishes_can_exclude_inactive_dishes(self) -> None:
        key = self._add_dish()
        dishes.set_active(key, False, db_path=self.db_path, actor=MANAGER)
        active = dishes.list_dishes(
            include_inactive=False, db_path=self.db_path, actor=MANAGER
        )
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["name"], "Regular Ramen")
        self.assertEqual(
            len(dishes.list_dishes(db_path=self.db_path, actor=MANAGER)), 2
        )

    def test_prices_are_formatted_as_php(self) -> None:
        self.assertEqual(CURRENCY_SYMBOL, "\u20b1")
        self.assertEqual(format_money(180), "\u20b1180.00")
        for relative in (
            "menu_repository.py", "pages/menu.py", "pages/recipe.py",
            "database/schema.sql",
        ):
            with self.subTest(file=relative):
                text = _source(relative)
                self.assertNotIn("\u00a5", text)
                self.assertNotIn("\uffe5", text)


class MenuPermissionTests(_TempDB):

    def test_cashier_cannot_add_a_dish(self) -> None:
        with self.assertRaises(permissions.PermissionDenied):
            dishes.add_dish(
                "Sneaky Ramen", "", "100",
                db_path=self.db_path, actor=CASHIER,
            )

    def test_cashier_cannot_read_or_write_dishes(self) -> None:
        calls = (
            lambda: dishes.list_dishes(db_path=self.db_path, actor=CASHIER),
            lambda: dishes.summary(db_path=self.db_path, actor=CASHIER),
            lambda: dishes.get_dish(1, db_path=self.db_path, actor=CASHIER),
            lambda: dishes.update_dish(
                1, "x", "", "100", db_path=self.db_path, actor=CASHIER
            ),
            lambda: dishes.set_active(
                1, False, db_path=self.db_path, actor=CASHIER
            ),
        )
        for call in calls:
            with self.subTest(call=call):
                with self.assertRaises(permissions.PermissionDenied):
                    call()

    def test_a_live_cashier_session_blocks_even_without_an_explicit_actor(
        self,
    ) -> None:
        permissions.set_session(CASHIER)
        with self.assertRaises(permissions.PermissionDenied):
            dishes.add_dish("Sneaky Ramen", "", "100", db_path=self.db_path)

    def test_manager_capability_is_registered(self) -> None:
        self.assertIn(
            permissions.CAP_MANAGE_MENU, permissions.ALL_CAPABILITIES
        )
        self.assertNotIn(
            permissions.CAP_MANAGE_MENU, permissions.CASHIER_CAPABILITIES
        )
        self.assertTrue(permissions.can(MANAGER, permissions.CAP_MANAGE_MENU))
        self.assertFalse(permissions.can(CASHIER, permissions.CAP_MANAGE_MENU))


# ----------------------------------------------------------------------
# 4. Per-dish recipes (bill of materials isolation)
# ----------------------------------------------------------------------
class PerDishRecipeTests(_TempDB):

    def test_new_dish_starts_with_an_empty_recipe(self) -> None:
        key = self._add_dish()
        self.assertEqual(
            recipes.list_recipe_lines(self.db_path, menu_item_id=key), []
        )
        header = recipes.get_recipe(self.db_path, menu_item_id=key)
        self.assertEqual(header["menu_item_name"], "Spicy Ramen")
        self.assertEqual(header["ingredient_count"], 0)

    def test_default_recipe_is_still_the_primary_dish(self) -> None:
        self._add_dish()
        header = recipes.get_recipe(self.db_path)
        self.assertEqual(header["menu_item_name"], "Regular Ramen")
        self.assertEqual(header["ingredient_count"], 8)
        self.assertEqual(len(recipes.list_recipe_lines(self.db_path)), 8)

    def test_adding_an_ingredient_to_dish_b_never_touches_dish_a(self) -> None:
        key = self._add_dish()
        before_a = recipes.list_recipe_lines(self.db_path)
        ingredient_id = self._query(
            "SELECT ingredient_id FROM ingredients WHERE name = 'Pork'"
        )[0]["ingredient_id"]
        result = recipes.add_ingredient(
            ingredient_id, "25", db_path=self.db_path,
            actor=MANAGER, menu_item_id=key,
        )
        self.assertEqual(result["name"], "Pork")
        self.assertEqual(result["quantity_per_serving"], 25.0)
        self.assertEqual(recipes.list_recipe_lines(self.db_path), before_a)
        lines_b = recipes.list_recipe_lines(self.db_path, menu_item_id=key)
        self.assertEqual(len(lines_b), 1)
        self.assertEqual(lines_b[0]["quantity_per_serving"], 25.0)

    def test_available_ingredients_are_scoped_per_dish(self) -> None:
        key = self._add_dish()
        self.assertEqual(
            recipes.list_available_ingredients(self.db_path), []
        )
        available_b = recipes.list_available_ingredients(
            self.db_path, menu_item_id=key
        )
        self.assertEqual(len(available_b), 8)
        self.assertIn("Pork", {row["name"] for row in available_b})

    def test_dishes_may_share_the_same_ingredient(self) -> None:
        key = self._add_dish()
        ingredient_id = self._query(
            "SELECT ingredient_id FROM ingredients WHERE name = 'Pork'"
        )[0]["ingredient_id"]
        recipes.add_ingredient(
            ingredient_id, "25", db_path=self.db_path,
            actor=MANAGER, menu_item_id=key,
        )
        self.assertEqual(
            self._scalar(
                "SELECT COUNT(*) FROM recipe_ingredients WHERE ingredient_id = ?",
                (ingredient_id,),
            ), 2
        )

    def test_notes_are_per_dish(self) -> None:
        key = self._add_dish()
        recipes.update_notes(
            "Extra chilli oil", db_path=self.db_path,
            actor=MANAGER, menu_item_id=key,
        )
        self.assertEqual(
            recipes.get_recipe(self.db_path, menu_item_id=key)["notes"],
            "Extra chilli oil",
        )
        self.assertEqual(
            recipes.get_recipe(self.db_path)["notes"],
            "Sample recipe - quantities are editable.",
        )

    def test_update_quantity_rejects_a_line_from_another_dish(self) -> None:
        key = self._add_dish()
        line_id = recipes.list_recipe_lines(self.db_path)[0][
            "recipe_ingredient_id"
        ]
        with self.assertRaisesRegex(ValueError, "no longer exists"):
            recipes.update_quantity(
                line_id, "999", db_path=self.db_path,
                actor=MANAGER, menu_item_id=key,
            )

    def test_remove_ingredient_rejects_a_line_from_another_dish(self) -> None:
        key = self._add_dish()
        line_id = recipes.list_recipe_lines(self.db_path)[0][
            "recipe_ingredient_id"
        ]
        with self.assertRaisesRegex(ValueError, "no longer exists"):
            recipes.remove_ingredient(
                line_id, db_path=self.db_path,
                actor=MANAGER, menu_item_id=key,
            )

    def test_recipe_edits_never_change_physical_stock(self) -> None:
        key = self._add_dish()
        before = self._query(
            "SELECT ingredient_id, quantity_on_hand FROM ingredients "
            "ORDER BY ingredient_id"
        )
        ingredient_id = before[0]["ingredient_id"]
        recipes.add_ingredient(
            ingredient_id, "10", db_path=self.db_path,
            actor=MANAGER, menu_item_id=key,
        )
        self.assertEqual(
            self._query(
                "SELECT ingredient_id, quantity_on_hand FROM ingredients "
                "ORDER BY ingredient_id"
            ), before
        )
        self.assertEqual(
            self._scalar("SELECT COUNT(*) FROM vw_stock_movements"), 0
        )

    def test_recipe_module_contains_no_stock_writes(self) -> None:
        source = _source("recipe_repository.py").lower()
        self.assertNotIn("update ingredients", source)
        self.assertNotIn("insert into stock_", source)
        self.assertNotIn("update stock_", source)


# ----------------------------------------------------------------------
# 5. POS and Calculator still use ONLY the primary dish
# ----------------------------------------------------------------------
class PosRegressionTests(_TempDB):

    def test_pos_context_ignores_other_dishes(self) -> None:
        key = self._add_dish()
        ingredient_id = self._query(
            "SELECT ingredient_id FROM ingredients WHERE name = 'Pork'"
        )[0]["ingredient_id"]
        recipes.add_ingredient(
            ingredient_id, "999", db_path=self.db_path,
            actor=MANAGER, menu_item_id=key,
        )
        context = sales.get_pos_context(self.db_path)
        self.assertEqual(context["menu_item_name"], "Regular Ramen")
        self.assertEqual(context["unit_price"], 150.0)
        self.assertEqual(len(context["lines"]), 8)
        self.assertNotIn(
            999.0,
            [float(line["quantity_per_serving"]) for line in context["lines"]],
        )

    def test_calculator_ignores_other_dishes(self) -> None:
        key = self._add_dish()
        ingredient_id = self._query(
            "SELECT ingredient_id FROM ingredients WHERE name = 'Pork'"
        )[0]["ingredient_id"]
        recipes.add_ingredient(
            ingredient_id, "999", db_path=self.db_path,
            actor=MANAGER, menu_item_id=key,
        )
        result = usage.calculate(1, db_path=self.db_path)
        self.assertEqual(result["menu_item_name"], "Regular Ramen")
        self.assertEqual(result["ingredient_count"], 8)

    def test_set_unit_price_updates_only_the_primary_dish(self) -> None:
        key = self._add_dish()
        self.assertEqual(
            sales.set_unit_price(
                "175", db_path=self.db_path, actor=MANAGER
            ), 175.0
        )
        self.assertEqual(
            self._scalar(
                "SELECT unit_price FROM menu_items WHERE menu_item_id = ?",
                (key,),
            ), 180.0
        )

    def test_a_real_sale_still_deducts_only_the_primary_recipe(self) -> None:
        key = self._add_dish()
        ingredient_id = self._query(
            "SELECT ingredient_id FROM ingredients WHERE name = 'Pork'"
        )[0]["ingredient_id"]
        recipes.add_ingredient(
            ingredient_id, "999", db_path=self.db_path,
            actor=MANAGER, menu_item_id=key,
        )
        self._stock_up(10)
        before = {
            row["ingredient_id"]: row["quantity_on_hand"]
            for row in self._query(
                "SELECT ingredient_id, quantity_on_hand FROM ingredients"
            )
        }
        result = sales.confirm_sale(
            2, "Cash", "500", db_path=self.db_path, actor=MANAGER
        )
        self.assertEqual(result["menu_item_name"], "Regular Ramen")
        self.assertEqual(result["total_amount"], 300.0)
        self.assertEqual(result["currency_code"], "PHP")
        deductions = self._query(
            "SELECT * FROM sale_deductions WHERE sale_id = ?",
            (result["sale_id"],),
        )
        self.assertEqual(len(deductions), 8)
        for line in deductions:
            self.assertLess(line["quantity_deducted"], 900)
            self.assertEqual(
                self._scalar(
                    "SELECT quantity_on_hand FROM ingredients "
                    "WHERE ingredient_id = ?",
                    (line["ingredient_id"],),
                ),
                before[line["ingredient_id"]] - line["quantity_deducted"],
            )

    def test_void_still_reverses_the_primary_sale(self) -> None:
        self._add_dish()
        self._stock_up(10)
        before = {
            row["ingredient_id"]: row["quantity_on_hand"]
            for row in self._query(
                "SELECT ingredient_id, quantity_on_hand FROM ingredients"
            )
        }
        result = sales.confirm_sale(
            1, "Cash", "500", db_path=self.db_path, actor=MANAGER
        )
        sales.void_sale(
            result["sale_id"], "Customer cancelled",
            db_path=self.db_path, actor=MANAGER,
        )
        after = {
            row["ingredient_id"]: row["quantity_on_hand"]
            for row in self._query(
                "SELECT ingredient_id, quantity_on_hand FROM ingredients"
            )
        }
        self.assertEqual(after, before)
        self.assertEqual(
            self._scalar(
                "SELECT status FROM sales_transactions WHERE sale_id = ?",
                (result["sale_id"],),
            ), "voided"
        )

    def test_dashboard_still_reports_the_primary_dish(self) -> None:
        self._add_dish()
        self.assertEqual(
            database.get_dashboard_data(self.db_path)["menu_item"],
            "Regular Ramen",
        )
        self.assertEqual(database.get_menu_item_name(self.db_path),
                         "Regular Ramen")

    def test_price_and_deduction_code_is_pinned_to_the_primary_dish(self) -> None:
        source = _source("sales_repository.py")
        self.assertIn("WHERE singleton = 1", source)
        self.assertIn("AND m.singleton = 1", source)
        usage_source = _source("usage_repository.py")
        self.assertEqual(usage_source.count("AND m.singleton = 1"), 2)


# ----------------------------------------------------------------------
# 6. UI wiring and permissions
# ----------------------------------------------------------------------
class UiWiringTests(_TempDB):

    def test_menu_page_is_manager_only_and_registered_in_order(self) -> None:
        self.assertEqual(tuple(PAGES), permissions.MANAGER_PAGES)
        self.assertIn("menu", PAGES)
        self.assertNotIn("menu", permissions.CASHIER_PAGES)
        self.assertEqual(PAGES["menu"]["label"], "Menu")
        self.assertEqual(PAGES["menu"]["view"].__name__, "MenuView")
        self.assertTrue(callable(PAGES["menu"]["view"].refresh))

    def test_main_entry_point_documents_ten_pages(self) -> None:
        self.assertIn("12 pages", _source("main.py"))

    def test_menu_page_has_a_denied_state(self) -> None:
        source = _source("pages/menu.py")
        self.assertIn("permissions.CAP_MANAGE_MENU", source)
        self.assertIn("Manager access required", source)
        self.assertIn("def refresh", source)

    def test_menu_page_guards_reads_and_writes(self) -> None:
        source = _source("pages/menu.py")
        self.assertIn("actor=self.actor", source)
        self.assertIn("+  Add dish", source)
        self.assertIn("format_money", source)

    def test_recipe_page_has_a_dish_selector(self) -> None:
        source = _source("pages/recipe.py")
        self.assertIn("dish_combo", source)
        self.assertIn("_on_dish_change", source)
        self.assertIn("menu_repository as dishes", source)
        self.assertIn("menu_item_id=self.selected_id", source)

    def test_recipe_repository_scopes_lines_by_recipe_id(self) -> None:
        source = _source("recipe_repository.py")
        self.assertIn("WHERE ri.recipe_id = ?", source)
        self.assertIn("m.singleton = 1", source)

    def test_menu_repository_guards_every_public_entry_point(self) -> None:
        """Every db-facing function must be manager-only, checked BEFORE SQL.

        A literal occurrence count goes stale as soon as a helper is added, so
        inspect the module instead: any public function accepting an ``actor``
        is a data entry point and must call permissions.require() before it
        opens a transaction. Pure validators (no actor) are intentionally
        excluded.
        """
        guarded: list[str] = []
        for name, function in inspect.getmembers(dishes, inspect.isfunction):
            if name.startswith("_"):
                continue
            if "actor" not in inspect.signature(function).parameters:
                continue
            body = inspect.getsource(function)
            self.assertIn(
                "permissions.require(actor, permissions.CAP_MANAGE_MENU)",
                body,
                f"{name}() must require the manager capability",
            )
            self.assertLess(
                body.index("permissions.require("),
                body.index("database.transaction("),
                f"{name}() must check permissions before touching SQLite",
            )
            guarded.append(name)
        self.assertEqual(
            set(guarded),
            {
                "list_dishes",
                "get_dish",
                "summary",
                "add_dish",
                "update_dish",
                "set_active",
            },
        )

    def test_sidebar_scales_to_ten_pages_without_clipping(self) -> None:
        """The 10th page must not push the nav list or exit footer off-screen."""
        source = _source("components/sidebar.py")
        self.assertIn("CTkScrollableFrame", source)
        self.assertIn("self.nav_frame", source)
        self.assertIn("self.exit_button", source)
        self.assertIn("fill=\"both\", expand=True", source)
        self.assertNotIn("# Flexible spacer pushes the footer", source)

    def test_schema_still_forbids_negative_stock_and_duplicate_active_names(
        self,
    ) -> None:
        schema = _source("database/schema.sql")
        self.assertIn("CHECK (quantity_on_hand >= 0)", schema)
        self.assertIn("ux_ingredients_active_name", schema)
        self.assertIn("ON DELETE RESTRICT", schema)
        self.assertIn("PRIMARY KEY AUTOINCREMENT", schema)


if __name__ == "__main__":
    unittest.main(verbosity=2)
