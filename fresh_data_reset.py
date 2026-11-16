"""
fresh_data_reset.py - One-time client deployment data reset
============================================================

Clears all demo product/catalog data and sales history from the
deployment database so the client can start with a clean slate.

WHAT GETS CLEARED (demo/seed data):
  - store_sale_items          (23 demo sale line items)
  - store_sales               (12 demo sales)
  - stock_out_details         (23 demo stock-out lines)
  - stock_out_transactions    (12 demo sale stock-outs)
  - stock_in_details          (18 demo opening-stock lines)
  - stock_in_transactions     (1 demo opening-stock batch)
  - sale_deductions           (legacy ramen-sale deductions, if any)
  - sales_transactions        (legacy ramen sales, if any)
  - recipe_ingredients        (8 demo recipe lines)
  - recipes                   (1 demo recipe)
  - menu_items                (1 demo menu item)
  - ingredients               (26 rows: 8 inactive ramen + 18 active store products)
  - stock_batches             (Stage 18 batch table, if migrated)
  - stock_batch_allocations   (Stage 18 allocation table, if migrated)
  - suppliers                 (Stage 18 supplier table, if migrated)

WHAT IS PRESERVED:
  - users                     (admin + cashier accounts, password hashes, roles)
  - app_meta                  (migration markers: stage12_schema, catalog_seeded, etc.)
  - user_version              (schema version = 4)
  - sqlite_sequence           (autoincrement counters, reset to 0 for a true fresh start)
  - All table definitions, indexes, triggers, views

SAFETY:
  1. Requires a verified backup before proceeding (sha256 + integrity_check).
  2. Foreign keys are ON during deletion (correct child-first order).
  3. Does NOT run on app startup - must be invoked explicitly.
  4. Prints a full before/after report.
  5. Verifies the app can initialize after reset.

Usage:
  .venv\\Scripts\\python.exe fresh_data_reset.py              # dry run (preview only)
  .venv\\Scripts\\python.exe fresh_data_reset.py --execute     # real reset
  .venv\\Scripts\\python.exe fresh_data_reset.py --db path.db  # target a specific DB
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# Import app modules for backup verification and schema initialization
import backups
import database
import products

# ------------------------------------------------------------------
# Constants
# ------------------------------------------------------------------

DEFAULT_DB_PATH = database.DB_PATH

# Tables to clear, in FK-safe child-first order.
# Each entry: (table_name, human_description)
CLEAR_TABLES: list[tuple[str, str]] = [
    # Stage 18 batch tables (child first)
    ("stock_batch_allocations", "batch allocations (FEFO sale allocations)"),
    ("stock_batches", "stock batches (lot/expiry tracking)"),
    ("suppliers", "suppliers"),
    # Store POS sale snapshots (child of store_sales + ingredients)
    ("store_sale_items", "store sale line items (immutable snapshots)"),
    # Store POS sales (child of users + stock_out_transactions)
    ("store_sales", "store sales (completed/voided)"),
    # Stock movement details (child of transactions + ingredients)
    ("stock_out_details", "stock-out detail lines"),
    ("stock_in_details", "stock-in detail lines"),
    # Stock movement headers (child of users)
    ("stock_out_transactions", "stock-out transactions"),
    ("stock_in_transactions", "stock-in transactions"),
    # Legacy ramen POS (child of ingredients + sales_transactions)
    ("sale_deductions", "legacy ramen sale deductions"),
    ("sales_transactions", "legacy ramen sales"),
    # Recipe linkage (child of recipes + ingredients)
    ("recipe_ingredients", "recipe ingredient lines"),
    # Recipes (child of menu_items)
    ("recipes", "recipes"),
    # Menu items (no FK parents in the clear set)
    ("menu_items", "menu items"),
    # Ingredients / products (parent of many, cleared last)
    ("ingredients", "products / ingredients (catalog + stock)"),
]

# Tables that are PRESERVED (never touched).
PRESERVE_TABLES: list[str] = [
    "users",
    "app_meta",
    "sqlite_sequence",
]

# app_meta keys to preserve (migration markers).
PRESERVE_META_KEYS: list[str] = [
    "stage12_schema",
    "catalog_seeded",
    "stage15_schema",
    "stage18_schema",
]


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Return {table_name: row_count} for all user tables."""
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]
    counts = {}
    for t in tables:
        counts[t] = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
    return counts


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _verify_backup(backup_path: Path) -> dict[str, Any]:
    """Verify a backup file is valid before we touch the live DB."""
    if not backup_path.is_file():
        raise ValueError(f"Backup file does not exist: {backup_path}")

    result: dict[str, Any] = {"path": str(backup_path)}
    result["sha256"] = _sha256(backup_path)
    result["size_bytes"] = backup_path.stat().st_size

    conn = sqlite3.connect(f"{backup_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        result["integrity_check"] = integrity
        if integrity != "ok":
            raise ValueError(
                f"Backup failed integrity check: {integrity}. Refusing to proceed."
            )

        # Verify expected tables exist
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        result["table_count"] = len(tables)
        result["tables"] = sorted(tables)

        # FK check
        broken = conn.execute("PRAGMA foreign_key_check").fetchall()
        result["fk_violations"] = len(broken)
        if broken:
            raise ValueError(
                f"Backup has {len(broken)} foreign key violations. Refusing to proceed."
            )

        result["user_version"] = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()

    result["verified"] = True
    return result


def _create_pre_reset_backup(db_path: Path) -> Path:
    """Create a verified backup of the live DB using SQLite's online backup API.

    This mirrors the approach in backups.py but bypasses the permission check
    (CAP_MANAGE_BACKUPS) because this script is run by the deployer, not
    through the app's RBAC layer. The backup uses the same WAL-safe
    sqlite3.Connection.backup() call.
    """
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"pre-reset-{stamp}.db"
    temp_path = backup_dir / f".{backup_path.name}.tmp"
    if temp_path.exists():
        temp_path.unlink()

    # Open source read-only (WAL-safe snapshot)
    source = sqlite3.connect(str(db_path), timeout=5.0)
    source.execute("PRAGMA query_only = ON")
    source.execute("PRAGMA busy_timeout = 5000")

    destination = sqlite3.connect(str(temp_path), timeout=5.0)
    try:
        source.backup(destination)
    except sqlite3.Error as exc:
        destination.close()
        source.close()
        raise ValueError(f"Could not back up the database: {exc}") from None
    finally:
        destination.close()
        source.close()

    # Verify the temp backup before renaming
    verification = _verify_backup(temp_path)
    if not verification.get("verified"):
        raise ValueError(
            f"Backup verification failed. Temp file kept at {temp_path}"
        )

    # Atomic rename
    os.replace(str(temp_path), str(backup_path))
    return backup_path


# ------------------------------------------------------------------
# Core reset logic
# ------------------------------------------------------------------

def preview(db_path: Path) -> dict[str, Any]:
    """Read the database and report what would be cleared vs preserved.

    Does NOT modify anything.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        counts = _row_counts(conn)
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]

        # Check which Stage 18 tables exist
        stage18_tables = {}
        for t in ["stock_batches", "stock_batch_allocations", "suppliers"]:
            stage18_tables[t] = _table_exists(conn, t)

        # Users
        users = [
            dict(r)
            for r in conn.execute(
                "SELECT user_id, username, role, is_active FROM users ORDER BY user_id"
            ).fetchall()
        ]

        # app_meta
        meta = [
            dict(r) for r in conn.execute("SELECT * FROM app_meta ORDER BY key").fetchall()
        ]

        # Active products
        products_list = [
            dict(r)
            for r in conn.execute(
                "SELECT ingredient_id, name, barcode, category, "
                "quantity_on_hand, unit_price "
                "FROM ingredients WHERE is_active = 1 "
                "ORDER BY category, name"
            ).fetchall()
        ]

        # Sales summary
        sales = [
            dict(r)
            for r in conn.execute(
                "SELECT sale_id, transaction_number, transaction_date, "
                "total_amount, status FROM store_sales ORDER BY sale_id"
            ).fetchall()
        ]

        # Build the clear/preserve breakdown
        clear_breakdown = []
        for table, desc in CLEAR_TABLES:
            exists = _table_exists(conn, table)
            count = counts.get(table, 0) if exists else 0
            clear_breakdown.append(
                {
                    "table": table,
                    "description": desc,
                    "exists": exists,
                    "rows": count,
                }
            )

        preserve_breakdown = []
        for table in PRESERVE_TABLES:
            exists = _table_exists(conn, table)
            count = counts.get(table, 0) if exists else 0
            preserve_breakdown.append(
                {
                    "table": table,
                    "rows": count,
                }
            )

        return {
            "db_path": str(db_path),
            "user_version": user_version,
            "stage18_tables": stage18_tables,
            "users": users,
            "app_meta": meta,
            "products": products_list,
            "sales": sales,
            "clear_breakdown": clear_breakdown,
            "preserve_breakdown": preserve_breakdown,
            "total_rows_to_clear": sum(
                item["rows"] for item in clear_breakdown
            ),
        }
    finally:
        conn.close()


def execute_reset(db_path: Path) -> dict[str, Any]:
    """Perform the actual data reset.

    Steps:
      1. Create + verify a backup.
      2. BEGIN IMMEDIATE transaction.
      3. Delete child-first through the FK chain.
      4. Reset sqlite_sequence for cleared tables.
      5. PRAGMA foreign_key_check.
      6. Commit.
      7. Re-run ensure_schema + bootstrap_catalog check.
    """
    # Step 1: Backup
    backup_path = _create_pre_reset_backup(db_path)

    # Step 2: Open write connection and reset
    conn = database.get_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")

        deleted = {}
        for table, desc in CLEAR_TABLES:
            if not _table_exists(conn, table):
                deleted[table] = 0
                continue
            count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            conn.execute(f'DELETE FROM "{table}"')
            deleted[table] = count

        # Reset autoincrement counters for a true fresh start
        # sqlite_sequence only exists if AUTOINCREMENT was used
        if _table_exists(conn, "sqlite_sequence"):
            for table, _ in CLEAR_TABLES:
                conn.execute(
                    "DELETE FROM sqlite_sequence WHERE name = ?", (table,)
                )

        # Verify FK integrity
        broken = conn.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            conn.rollback()
            raise ValueError(
                f"FK violations after reset: {broken}. Rolled back. "
                f"Backup is at {backup_path}"
            )

        # Verify integrity
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            conn.rollback()
            raise ValueError(
                f"Integrity check failed after reset: {integrity}. Rolled back. "
                f"Backup is at {backup_path}"
            )

        conn.commit()

        # Post-reset verification
        post_counts = _row_counts(conn)

        # Re-run schema + catalog bootstrap to set up the empty catalog
        # (ensure_schema is idempotent, bootstrap_catalog checks catalog_seeded)
        conn.close()
        products.ensure_schema(db_path=str(db_path))

        return {
            "ok": True,
            "backup_path": str(backup_path),
            "deleted": deleted,
            "total_deleted": sum(deleted.values()),
            "post_counts": post_counts,
        }
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
        raise


def verify_app_starts(db_path: Path) -> dict[str, Any]:
    """Verify the app can initialize the database after reset."""
    try:
        # initialize_database runs schema + seed check (won't reseed at v4)
        database.initialize_database(db_path=str(db_path))
        # ensure_schema is also safe to run
        products.ensure_schema(db_path=str(db_path))

        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            counts = _row_counts(conn)
            users = [
                dict(r)
                for r in conn.execute(
                    "SELECT user_id, username, role, is_active FROM users"
                ).fetchall()
            ]
            user_version = conn.execute("PRAGMA user_version").fetchone()[0]
            integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])

            # Verify sales/product tables are empty
            empty_tables = []
            for table, _ in CLEAR_TABLES:
                if table in counts:
                    if counts[table] == 0:
                        empty_tables.append(table)
                    else:
                        pass  # Will be flagged

            return {
                "ok": True,
                "user_version": user_version,
                "integrity": integrity,
                "users": users,
                "counts": counts,
                "empty_data_tables": empty_tables,
                "all_data_cleared": all(
                    counts.get(table, 0) == 0 for table, _ in CLEAR_TABLES
                ),
            }
        finally:
            conn.close()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------

def _print_preview(report: dict[str, Any]) -> None:
    print("=" * 72)
    print("FRESH DATA RESET - PREVIEW (DRY RUN)")
    print("=" * 72)
    print()
    print(f"Database:     {report['db_path']}")
    print(f"user_version: {report['user_version']}")
    print(f"Stage 18 migrated: {any(report['stage18_tables'].values())}")
    print()

    print("-" * 72)
    print("USERS (PRESERVED):")
    for u in report["users"]:
        print(
            f"  #{u['user_id']} {u['username']:12s}  role={u['role']:8s}  "
            f"active={u['is_active']}"
        )
    print()

    print("-" * 72)
    print("APP_META (PRESERVED):")
    for m in report["app_meta"]:
        print(f"  {m['key']} = {m['value']}")
    print()

    print("-" * 72)
    print("ACTIVE PRODUCTS (TO BE CLEARED):")
    if not report["products"]:
        print("  (none)")
    for p in report["products"]:
        print(
            f"  #{p['ingredient_id']:3d}  {p['name'][:35]:35s}  "
            f"{p['barcode']:14s}  {p['category']:14s}  "
            f"stock={p['quantity_on_hand']:6.0f}  price={p['unit_price']:6.0f}"
        )
    print()

    print("-" * 72)
    print("STORE SALES (TO BE CLEARED):")
    if not report["sales"]:
        print("  (none)")
    for s in report["sales"]:
        print(
            f"  #{s['sale_id']:3d}  {s['transaction_number']:20s}  "
            f"{s['transaction_date']:20s}  {s['total_amount']:8.2f}  "
            f"{s['status']}"
        )
    print()

    print("-" * 72)
    print("TABLES TO BE CLEARED (FK child-first order):")
    total = 0
    for item in report["clear_breakdown"]:
        status = f"{item['rows']:5d} rows" if item["exists"] else "(not yet created)"
        print(f"  {item['table']:30s}  {item['description']:40s}  {status}")
        if item["exists"]:
            total += item["rows"]
    print(f"  {'TOTAL':30s}  {'':40s}  {total:5d} rows")
    print()

    print("-" * 72)
    print("TABLES PRESERVED:")
    for item in report["preserve_breakdown"]:
        print(f"  {item['table']:30s}  {item['rows']:5d} rows")
    print()

    print("=" * 72)
    print("To execute:  python fresh_data_reset.py --execute")
    print("=" * 72)


def _print_result(result: dict[str, Any]) -> None:
    print()
    print("=" * 72)
    print("RESET COMPLETE")
    print("=" * 72)
    print(f"Backup: {result['backup_path']}")
    print(f"Total rows deleted: {result['total_deleted']}")
    print()
    print("Per-table breakdown:")
    for table, count in result["deleted"].items():
        print(f"  {table:30s}  {count:5d} deleted")
    print()
    print("Post-reset counts:")
    for table, count in sorted(result["post_counts"].items()):
        print(f"  {table:30s}  {count:5d} rows")
    print()


def _print_verification(verify: dict[str, Any]) -> None:
    print("=" * 72)
    print("POST-RESET VERIFICATION")
    print("=" * 72)
    if not verify["ok"]:
        print(f"FAILED: {verify.get('error')}")
        return

    print(f"App initialization:   OK")
    print(f"Integrity check:      {verify['integrity']}")
    print(f"user_version:         {verify['user_version']}")
    print(f"All data tables empty: {verify['all_data_cleared']}")
    print()
    print("Users preserved:")
    for u in verify["users"]:
        print(
            f"  #{u['user_id']} {u['username']:12s}  role={u['role']:8s}  "
            f"active={u['is_active']}"
        )
    print()
    print("Final table counts:")
    for table, count in sorted(verify["counts"].items()):
        print(f"  {table:30s}  {count:5d} rows")
    print()

    if verify["all_data_cleared"]:
        print("RESULT: All demo data cleared. Client can enter their own data.")
    else:
        print("WARNING: Some data tables are not empty. Check the counts above.")
    print("=" * 72)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="One-time fresh-data reset for client deployment.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python fresh_data_reset.py              # Dry run - preview what will be cleared
  python fresh_data_reset.py --execute    # Perform the reset (requires backup)
  python fresh_data_reset.py --db path.db # Target a specific database file
""",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually perform the reset (default: dry run / preview only)",
    )
    parser.add_argument(
        "--db",
        type=str,
        default=str(DEFAULT_DB_PATH),
        help=f"Database file path (default: {DEFAULT_DB_PATH})",
    )
    args = parser.parse_args()

    db_path = Path(args.db).resolve()
    if not db_path.is_file():
        print(f"ERROR: Database file not found: {db_path}")
        return 1

    print(f"Target database: {db_path}")
    print(f"Mode: {'EXECUTE' if args.execute else 'DRY RUN (preview)'}")
    print()

    # Always preview first
    report = preview(db_path)
    _print_preview(report)

    if not args.execute:
        return 0

    # Execute
    print()
    print("=" * 72)
    print("EXECUTING RESET...")
    print("=" * 72)
    print()

    result = execute_reset(db_path)
    _print_result(result)

    # Verify app starts
    print("Verifying app can initialize...")
    verify = verify_app_starts(db_path)
    _print_verification(verify)

    return 0 if verify.get("ok") and verify.get("all_data_cleared") else 1


if __name__ == "__main__":
    sys.exit(main())
