"""
Stage 18 - batch / lot inventory, suppliers and purchase-cost snapshots.

This module is ADDITIVE and SELF-CONTAINED. It does not change database.py,
schema.sql or PRAGMA user_version (still 4). Every statement is
CREATE ... IF NOT EXISTS / guarded ALTER TABLE ADD COLUMN, so calling
ensure_schema() on every launch - or at the start of every write
transaction - is safe and idempotent.

Data model
----------
  suppliers                 one row per vendor (optional on a stock-in header)
  stock_batches              one row per RECEIPT of stock. Holds received_qty,
                             remaining_qty, an OPTIONAL expiry date and an
                             OPTIONAL unit purchase cost in Philippine peso.
  stock_batch_allocations   one row per (stock-out detail line, batch) pair.
                             Snapshots the batch's unit cost at the moment of
                             the sale so later price edits cannot rewrite
                             cost-of-goods-sold history. A void restores the
                             SAME batch ids and records restored_quantity.

Allocation order (FEFO)
-----------------------
  Batches with a known expiry date come first, earliest date first. Batches
  with NO expiry date come afterwards, oldest received_at first (a NULL
  received_at - legacy opening stock - sorts first, because its true receipt
  date is unknown and it is the oldest stock we know about). batch_id is the
  final deterministic tie-breaker.

Unknown historical values
-------------------------
  Existing products are migrated with ONE opening batch each, whose
  remaining_qty equals the product's current quantity_on_hand. Its
  expiry date, unit cost, supplier and received_at are ALL NULL: unknown,
  never guessed. The original stock-in lines (including any historical expiry
  dates and reference numbers) are left completely untouched. Back-projecting
  those per-line dates onto today's remaining balance would require guessing
  which delivery each surviving unit came from, so it is deliberately NOT
  done. That is an owner decision, recorded in docs/stage18_decisions.md.

Exact money (deferred, owner decision)
-------------------------------------
  Monetary columns here stay SQLite REAL, exactly like every other money
  column in the project. Converting to integer cents would require rewriting
  the 814-test regression suite, the receipt archiver, the Z-read, the CSV
  export and every immutable money snapshot, and could shift a centavo on
  historical 20% discounts. It is NOT safe to do inside an additive migration
  whose hard requirement is "no historical total changes". Deferred until the
  owner explicitly approves a dedicated, separately verified migration stage.

Customer ID data
----------------
  Full Senior/PWD ID numbers stay on the sale row and the receipt (the
  discount evidence). Sales reports, Z-reads and CSV exports no longer carry
  them; see sales_report.py and docs/stage18_decisions.md. No retention
  period has been invented - that is an owner decision.

All money is Philippine peso (PHP). No other currency is used.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable

import database
import permissions

STAGE18_VERSION = "18"
EPSILON = 1e-6

# Opening-batch marker used for both the legacy migration and the unknown
# provenance of a pre-batch sale that is voided later.
OPENING_NOTE = "Opening balance - expiry, cost and supplier unknown"
UNKNOWN_REVERSAL_NOTE = "Void of a pre-batch sale - original batch unknown"

# DDL is executed ONE statement at a time inside the caller's BEGIN IMMEDIATE
# transaction. executescript() would implicitly COMMIT and break atomicity.
_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS suppliers (
        supplier_id   INTEGER PRIMARY KEY AUTOINCREMENT,
        name          TEXT    NOT NULL,
        contact_info  TEXT    NOT NULL DEFAULT '',
        notes         TEXT    NOT NULL DEFAULT '',
        is_active     INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
        created_at    TEXT    NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_suppliers_active_name "
    "ON suppliers (name COLLATE NOCASE) WHERE is_active = 1",
    """
    CREATE TABLE IF NOT EXISTS stock_batches (
        batch_id            INTEGER PRIMARY KEY AUTOINCREMENT,
        ingredient_id       INTEGER NOT NULL
                            REFERENCES ingredients (ingredient_id) ON DELETE RESTRICT,
        stock_in_detail_id  INTEGER
                            REFERENCES stock_in_details (stock_in_detail_id)
                            ON DELETE SET NULL,
        supplier_id         INTEGER
                            REFERENCES suppliers (supplier_id) ON DELETE SET NULL,
        received_qty        REAL    NOT NULL CHECK (received_qty > 0),
        remaining_qty       REAL    NOT NULL
                            CHECK (remaining_qty >= 0
                                   AND remaining_qty <= received_qty + 0.000001),
        expiration_date     TEXT,
        unit_cost           REAL    CHECK (unit_cost IS NULL OR unit_cost >= 0),
        received_at         TEXT,
        is_opening          INTEGER NOT NULL DEFAULT 0
                            CHECK (is_opening IN (0, 1)),
        notes               TEXT    NOT NULL DEFAULT '',
        created_at          TEXT    NOT NULL,
        updated_at          TEXT    NOT NULL,
        CHECK (expiration_date IS NULL
               OR expiration_date GLOB
                  '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_stock_batches_fefo "
    "ON stock_batches (ingredient_id, expiration_date, received_at, batch_id)",
    "CREATE INDEX IF NOT EXISTS ix_stock_batches_supplier "
    "ON stock_batches (supplier_id)",
    """
    CREATE TABLE IF NOT EXISTS stock_batch_allocations (
        allocation_id        INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id             INTEGER NOT NULL
                             REFERENCES stock_batches (batch_id) ON DELETE RESTRICT,
        stock_out_detail_id  INTEGER NOT NULL
                             REFERENCES stock_out_details (stock_out_detail_id)
                             ON DELETE RESTRICT,
        -- Soft reference on purpose: some legacy databases predate store_sales.
        -- A hard FK here would make the additive migration fail on those DBs.
        sale_id              INTEGER,
        quantity             REAL    NOT NULL CHECK (quantity > 0),
        unit_cost            REAL    CHECK (unit_cost IS NULL OR unit_cost >= 0),
        restored_quantity    REAL    NOT NULL DEFAULT 0
                             CHECK (restored_quantity >= 0
                                    AND restored_quantity <= quantity + 0.000001),
        restored_at          TEXT,
        created_at           TEXT    NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_batch_allocations_batch "
    "ON stock_batch_allocations (batch_id)",
    "CREATE INDEX IF NOT EXISTS ix_batch_allocations_detail "
    "ON stock_batch_allocations (stock_out_detail_id)",
    "CREATE INDEX IF NOT EXISTS ix_batch_allocations_sale "
    "ON stock_batch_allocations (sale_id)",
)

# Additive columns on EXISTING tables. Guarded by PRAGMA table_info, so a
# database created before Stage 18 is upgraded in place with safe NULL
# defaults; nothing is rewritten and every old reference/notes value stays.
_ADDITIVE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    (
        "stock_in_transactions",
        "supplier_id",
        "ALTER TABLE stock_in_transactions ADD COLUMN supplier_id INTEGER "
        "REFERENCES suppliers (supplier_id) ON DELETE SET NULL",
    ),
    (
        "stock_in_details",
        "unit_cost",
        "ALTER TABLE stock_in_details ADD COLUMN unit_cost REAL "
        "CHECK (unit_cost IS NULL OR unit_cost >= 0)",
    ),
    (
        "stock_in_details",
        "batch_id",
        "ALTER TABLE stock_in_details ADD COLUMN batch_id INTEGER "
        "REFERENCES stock_batches (batch_id) ON DELETE SET NULL",
    ),
)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(row) for row in cursor.fetchall()]


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _round(value: float) -> float:
    """Kill float dust such as 0.30000000000000004 in balances."""
    return round(float(value), 6)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone() is not None


# ----------------------------------------------------------------------
# Additive, idempotent schema
# ----------------------------------------------------------------------
def ensure_schema(conn: sqlite3.Connection) -> list[str]:
    """Create the Stage 18 tables/columns/indexes inside the caller's txn.

    Safe on a brand-new v4 database, on a Stage 12 database and on a fully
    migrated one. Returns the list of columns actually added.
    """
    for statement in _DDL:
        conn.execute(statement)
    added: list[str] = []
    for table, column, statement in _ADDITIVE_COLUMNS:
        if column not in _columns(conn, table):
            conn.execute(statement)
            added.append(f"{table}.{column}")
    return added


def backfill_unbatched(conn: sqlite3.Connection) -> int:
    """Give every ACTIVE product with no batch at all ONE opening batch.

    This is the backwards-compatible migration for existing stock AND the
    self-healing path for balances created by legacy code that wrote
    quantity_on_hand directly. Expiry, cost, supplier and received_at stay
    NULL (unknown, never guessed). Idempotent: once a product has any batch,
    it is skipped - even a fully consumed one - so this can run inside every
    write transaction.
    """
    cursor = conn.execute(
        """
        INSERT INTO stock_batches
            (ingredient_id, stock_in_detail_id, supplier_id,
             received_qty, remaining_qty, expiration_date, unit_cost,
             received_at, is_opening, notes, created_at, updated_at)
        SELECT i.ingredient_id, NULL, NULL,
               i.quantity_on_hand, i.quantity_on_hand, NULL, NULL,
               NULL, 1, ?, ?, ?
        FROM ingredients AS i
        WHERE i.is_active = 1
          AND i.quantity_on_hand > 0
          AND NOT EXISTS (
              SELECT 1 FROM stock_batches AS b
              WHERE b.ingredient_id = i.ingredient_id
          )
        """,
        (OPENING_NOTE, database.local_timestamp(), database.local_timestamp()),
    )
    return int(cursor.rowcount or 0)


def ensure_write_ready(conn: sqlite3.Connection) -> int:
    """DDL + opening-batch backfill at the start of a write transaction."""
    ensure_schema(conn)
    return backfill_unbatched(conn)


def migrate(db_path: Any = None, actor: Any = None) -> dict[str, Any]:
    """Explicit launch-time migration. Never touches users or old rows."""
    permissions.require(actor, permissions.CAP_MANAGE_BATCHES)
    with database.transaction(db_path) as conn:
        added = ensure_schema(conn)
        opening = backfill_unbatched(conn)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    return {
        "added_columns": added,
        "opening_batches": opening,
        "stage18_version": STAGE18_VERSION,
        "user_version": int(version),
    }


# ----------------------------------------------------------------------
# Suppliers
# ----------------------------------------------------------------------
def add_supplier(
    name: Any,
    contact_info: Any = "",
    notes: Any = "",
    db_path: Any = None,
    actor: Any = None,
) -> int:
    """Create one supplier. Manager only; duplicate active names refused."""
    permissions.require(actor, permissions.CAP_MANAGE_BATCHES)
    clean_name = " ".join(str(name if name is not None else "").split())
    if not clean_name:
        raise ValueError("Supplier name is required.")
    if len(clean_name) > 80:
        raise ValueError("Supplier name cannot be longer than 80 characters.")
    contact = " ".join(str(contact_info if contact_info is not None else "").split())
    clean_notes = " ".join(str(notes if notes is not None else "").split())
    if len(contact) > 120 or len(clean_notes) > 200:
        raise ValueError("Supplier contact or notes are too long.")

    with database.transaction(db_path) as conn:
        ensure_schema(conn)
        exists = conn.execute(
            "SELECT 1 FROM suppliers WHERE name = ? COLLATE NOCASE "
            "AND is_active = 1",
            (clean_name,),
        ).fetchone()
        if exists:
            raise ValueError("An active supplier with that name already exists.")
        cursor = conn.execute(
            "INSERT INTO suppliers (name, contact_info, notes, created_at) "
            "VALUES (?, ?, ?, ?)",
            (clean_name, contact, clean_notes, database.local_timestamp()),
        )
    return int(cursor.lastrowid)


def list_suppliers(
    db_path: Any = None,
    include_inactive: bool = False,
    actor: Any = None,
) -> list[dict[str, Any]]:
    """Suppliers for the stock-in picker (manager screens only)."""
    permissions.require(actor, permissions.CAP_MANAGE_BATCHES)
    conn = database.get_connection(db_path)
    try:
        ensure_schema(conn)
        where = "" if include_inactive else "WHERE is_active = 1"
        return _rows(conn.execute(
            "SELECT supplier_id, name, contact_info, notes, is_active, "
            f"created_at FROM suppliers {where} ORDER BY name"
        ))
    finally:
        conn.close()


# ----------------------------------------------------------------------
# Batches (write helpers take an OPEN transaction's connection)
# ----------------------------------------------------------------------
def create_batch(
    conn: sqlite3.Connection,
    *,
    ingredient_id: int,
    quantity: float,
    expiration_date: Any = None,
    unit_cost: Any = None,
    supplier_id: Any = None,
    stock_in_detail_id: Any = None,
    received_at: Any = None,
    is_opening: bool = False,
    notes: str = "",
) -> int:
    """Record one receipt of stock as one batch. Caller owns the txn."""
    amount = _round(float(quantity))
    if amount <= 0:
        raise ValueError("A batch must be received with a positive quantity.")
    cost = None if unit_cost is None or str(unit_cost).strip() == "" else _round(
        float(unit_cost)
    )
    if cost is not None and cost < 0:
        raise ValueError("Purchase cost cannot be negative.")
    expiry = str(expiration_date).strip() if expiration_date is not None else ""
    timestamp = database.local_timestamp()
    cursor = conn.execute(
        """
        INSERT INTO stock_batches
            (ingredient_id, stock_in_detail_id, supplier_id,
             received_qty, remaining_qty, expiration_date, unit_cost,
             received_at, is_opening, notes, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(ingredient_id), stock_in_detail_id, supplier_id,
            amount, amount, expiry or None, cost,
            received_at, 1 if is_opening else 0, notes, timestamp, timestamp,
        ),
    )
    return int(cursor.lastrowid)


def fefo_batches(
    conn: sqlite3.Connection, ingredient_id: int
) -> list[dict[str, Any]]:
    """Batches with stock left, in the documented FEFO order."""
    return _rows(conn.execute(
        """
        SELECT batch_id, ingredient_id, remaining_qty, received_qty,
               expiration_date, unit_cost, received_at, is_opening
        FROM stock_batches
        WHERE ingredient_id = ? AND remaining_qty > 0
        ORDER BY (expiration_date IS NULL) ASC,
                 expiration_date ASC,
                 received_at ASC,
                 batch_id ASC
        """,
        (int(ingredient_id),),
    ))


def allocate(
    conn: sqlite3.Connection,
    *,
    ingredient_id: int,
    quantity: float,
    stock_out_detail_id: int,
    sale_id: Any = None,
) -> list[dict[str, Any]]:
    """Consume stock from FEFO batches and record every allocation.

    Raises ValueError BEFORE writing anything when the batches cannot cover
    the quantity, so the surrounding transaction rolls back completely.
    """
    requested = _round(float(quantity))
    if requested <= 0:
        raise ValueError("A stock-out quantity must be positive.")
    candidates = fefo_batches(conn, ingredient_id)
    available = sum(_round(float(row["remaining_qty"])) for row in candidates)
    if requested > available + EPSILON:
        row = conn.execute(
            "SELECT name FROM ingredients WHERE ingredient_id = ?",
            (int(ingredient_id),),
        ).fetchone()
        name = str(row[0]) if row else f"product #{ingredient_id}"
        raise ValueError(
            f"Not enough batch stock for {name}: {available:g} available, "
            f"{requested:g} requested."
        )

    left = requested
    timestamp = database.local_timestamp()
    result: list[dict[str, Any]] = []
    for batch in candidates:
        if left <= EPSILON:
            break
        take = min(_round(float(batch["remaining_qty"])), left)
        if take <= 0:
            continue
        new_balance = max(0.0, _round(float(batch["remaining_qty"]) - take))
        conn.execute(
            "UPDATE stock_batches SET remaining_qty = ?, updated_at = ? "
            "WHERE batch_id = ?",
            (new_balance, timestamp, int(batch["batch_id"])),
        )
        cursor = conn.execute(
            """
            INSERT INTO stock_batch_allocations
                (batch_id, stock_out_detail_id, sale_id, quantity,
                 unit_cost, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                int(batch["batch_id"]), int(stock_out_detail_id), sale_id,
                take, batch["unit_cost"], timestamp,
            ),
        )
        result.append(
            {
                "allocation_id": int(cursor.lastrowid),
                "batch_id": int(batch["batch_id"]),
                "quantity": take,
                "unit_cost": batch["unit_cost"],
                "expiration_date": batch["expiration_date"],
            }
        )
        left = _round(left - take)
    return result


def restore_stock_out(
    conn: sqlite3.Connection,
    *,
    stock_out_id: int,
    expected_lines: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Restore a voided stock-out to the EXACT batches it consumed.

    Legacy sales recorded before Stage 18 have no allocations. For those we
    create an unknown-provenance opening batch (NULL expiry/cost) so the
    product balance and the batch ledger cannot drift apart. Nothing about
    the original sale, receipt or stock-in line is changed.
    """
    timestamp = database.local_timestamp()
    rows = _rows(conn.execute(
        """
        SELECT a.allocation_id, a.batch_id, a.quantity,
               a.restored_quantity, b.ingredient_id, b.received_qty,
               b.remaining_qty
        FROM stock_batch_allocations AS a
        JOIN stock_out_details AS d
          ON d.stock_out_detail_id = a.stock_out_detail_id
        JOIN stock_batches AS b ON b.batch_id = a.batch_id
        WHERE d.stock_out_id = ?
          AND a.restored_quantity < a.quantity - 0.000001
        ORDER BY a.allocation_id
        """,
        (int(stock_out_id),),
    ))
    restored_by_product: dict[int, float] = {}
    for row in rows:
        delta = _round(float(row["quantity"]) - float(row["restored_quantity"]))
        new_balance = _round(float(row["remaining_qty"]) + delta)
        if new_balance > float(row["received_qty"]) + EPSILON:
            raise ValueError(
                "Void reversal would exceed a batch's received quantity."
            )
        conn.execute(
            "UPDATE stock_batches SET remaining_qty = ?, updated_at = ? "
            "WHERE batch_id = ?",
            (new_balance, timestamp, int(row["batch_id"])),
        )
        conn.execute(
            "UPDATE stock_batch_allocations "
            "SET restored_quantity = quantity, restored_at = ? "
            "WHERE allocation_id = ?",
            (timestamp, int(row["allocation_id"])),
        )
        key = int(row["ingredient_id"])
        restored_by_product[key] = _round(
            restored_by_product.get(key, 0.0) + delta
        )

    unknown: list[dict[str, Any]] = []
    for line in expected_lines:
        ingredient_id = int(line["ingredient_id"])
        expected = _round(float(line["quantity"]))
        shortfall = _round(expected - restored_by_product.get(ingredient_id, 0.0))
        if shortfall > EPSILON:
            batch_id = create_batch(
                conn,
                ingredient_id=ingredient_id,
                quantity=shortfall,
                is_opening=True,
                notes=UNKNOWN_REVERSAL_NOTE,
            )
            unknown.append(
                {"ingredient_id": ingredient_id, "quantity": shortfall,
                 "batch_id": batch_id}
            )
    return {
        "restored_allocations": len(rows),
        "batches_restored": len({int(row["batch_id"]) for row in rows}),
        "unknown_restorations": unknown,
    }


# ----------------------------------------------------------------------
# Reads: batches, reconciliation, valuation / COGS support
# ----------------------------------------------------------------------
def list_batches(
    db_path: Any = None,
    ingredient_id: Any = None,
    include_empty: bool = False,
    actor: Any = None,
) -> list[dict[str, Any]]:
    """Batch ledger in FEFO order, with product and supplier names."""
    permissions.require(actor, permissions.CAP_MANAGE_BATCHES)
    conn = database.get_connection(db_path)
    try:
        ensure_schema(conn)
        clauses: list[str] = []
        params: list[Any] = []
        if not include_empty:
            clauses.append("b.remaining_qty > 0")
        if ingredient_id is not None:
            clauses.append("b.ingredient_id = ?")
            params.append(int(ingredient_id))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return _rows(conn.execute(
            "SELECT b.batch_id, b.ingredient_id, i.name AS product_name, "
            "i.unit, b.received_qty, b.remaining_qty, b.expiration_date, "
            "b.unit_cost, b.received_at, b.is_opening, b.notes, "
            "b.supplier_id, COALESCE(s.name, '-') AS supplier_name "
            "FROM stock_batches AS b "
            "JOIN ingredients AS i ON i.ingredient_id = b.ingredient_id "
            "LEFT JOIN suppliers AS s ON s.supplier_id = b.supplier_id "
            f"{where} "
            "ORDER BY (b.expiration_date IS NULL) ASC, "
            "b.expiration_date ASC, b.received_at ASC, b.batch_id ASC",
            params,
        ))
    finally:
        conn.close()


def reconcile(db_path: Any = None, actor: Any = None) -> dict[str, Any]:
    """Report mismatches between batch totals and product balances.

    Two independent checks:
      1. SUM(stock_batches.remaining_qty) must equal quantity_on_hand.
      2. received_qty - remaining_qty must equal the NET consumed quantity
         (allocations minus anything restored by a void) for each batch.
    Read-only: it reports problems, it never silently repairs them.
    """
    permissions.require(actor, permissions.CAP_MANAGE_BATCHES)
    conn = database.get_connection(db_path)
    try:
        ensure_schema(conn)
        product_rows = _rows(conn.execute(
            """
            SELECT i.ingredient_id, i.name, i.is_active,
                   i.quantity_on_hand,
                   COALESCE(SUM(b.remaining_qty), 0) AS batch_total
            FROM ingredients AS i
            LEFT JOIN stock_batches AS b
              ON b.ingredient_id = i.ingredient_id
            GROUP BY i.ingredient_id, i.name, i.is_active, i.quantity_on_hand
            ORDER BY i.ingredient_id
            """
        ))
        batch_rows = _rows(conn.execute(
            """
            SELECT b.batch_id, b.ingredient_id, b.received_qty,
                   b.remaining_qty,
                   COALESCE(SUM(a.quantity - a.restored_quantity), 0)
                       AS consumed
            FROM stock_batches AS b
            LEFT JOIN stock_batch_allocations AS a
              ON a.batch_id = b.batch_id
            GROUP BY b.batch_id, b.ingredient_id, b.received_qty,
                     b.remaining_qty
            ORDER BY b.batch_id
            """
        ))
    finally:
        conn.close()

    product_mismatches = [
        {
            "ingredient_id": int(row["ingredient_id"]),
            "name": str(row["name"]),
            "is_active": int(row["is_active"]),
            "on_hand": _round(row["quantity_on_hand"]),
            "batch_total": _round(row["batch_total"]),
            "difference": _round(row["quantity_on_hand"] - row["batch_total"]),
        }
        for row in product_rows
        if abs(float(row["quantity_on_hand"]) - float(row["batch_total"])) > EPSILON
    ]
    batch_mismatches = [
        {
            "batch_id": int(row["batch_id"]),
            "ingredient_id": int(row["ingredient_id"]),
            "expected_consumed": _round(row["received_qty"] - row["remaining_qty"]),
            "recorded_consumed": _round(row["consumed"]),
        }
        for row in batch_rows
        if abs(
            float(row["received_qty"]) - float(row["remaining_qty"])
            - float(row["consumed"])
        ) > EPSILON
    ]
    return {
        "ok": not product_mismatches and not batch_mismatches,
        "products_checked": len(product_rows),
        "batches_checked": len(batch_rows),
        "product_mismatches": product_mismatches,
        "batch_mismatches": batch_mismatches,
    }


def inventory_valuation(
    db_path: Any = None, actor: Any = None
) -> dict[str, Any]:
    """Data support for valuation: known-cost value + unknown-cost quantity.

    NOT a report screen. Batches with a NULL unit_cost are reported
    separately instead of being valued at zero, so a missing cost can never
    be mistaken for a free item.
    """
    permissions.require(actor, permissions.CAP_VIEW_REPORTS)
    conn = database.get_connection(db_path)
    try:
        ensure_schema(conn)
        rows = _rows(conn.execute(
            """
            SELECT b.ingredient_id, i.name AS product_name,
                   COALESCE(SUM(CASE WHEN b.unit_cost IS NOT NULL
                               THEN b.remaining_qty * b.unit_cost ELSE 0 END),
                             0) AS known_value,
                   COALESCE(SUM(CASE WHEN b.unit_cost IS NULL
                               THEN b.remaining_qty ELSE 0 END),
                             0) AS unknown_cost_qty,
                   COALESCE(SUM(b.remaining_qty), 0) AS remaining_qty
            FROM stock_batches AS b
            JOIN ingredients AS i ON i.ingredient_id = b.ingredient_id
            GROUP BY b.ingredient_id, i.name
            ORDER BY i.name
            """
        ))
    finally:
        conn.close()
    for row in rows:
        row["known_value"] = round(float(row["known_value"]), 2)
        row["unknown_cost_qty"] = _round(row["unknown_cost_qty"])
        row["remaining_qty"] = _round(row["remaining_qty"])
    return {
        "rows": rows,
        "known_value": round(sum(r["known_value"] for r in rows), 2),
        "unknown_cost_qty": _round(sum(r["unknown_cost_qty"] for r in rows)),
    }


def cost_of_goods_sold(
    date_value: Any = None, db_path: Any = None, actor: Any = None
) -> dict[str, Any]:
    """Data support for COGS: net allocated quantity x batch cost snapshot.

    Only COMPLETED store sales count, and a voided allocation is excluded via
    restored_quantity. Batches with an unknown cost contribute zero to the
    value but are counted in unknown_cost_qty. Legacy databases without the
    store_sales table simply report an empty result.
    """
    permissions.require(actor, permissions.CAP_VIEW_REPORTS)
    conn = database.get_connection(db_path)
    try:
        ensure_schema(conn)
        if not _table_exists(conn, "store_sales"):
            return {"value": 0.0, "quantity": 0.0, "unknown_cost_qty": 0.0,
                    "allocation_count": 0}
        params: list[Any] = []
        where = "s.status = 'completed'"
        if date_value is not None:
            where += " AND substr(s.transaction_date, 1, 10) = ?"
            params.append(str(date_value)[:10])
        row = conn.execute(
            """
            SELECT COALESCE(SUM((a.quantity - a.restored_quantity)
                                * COALESCE(a.unit_cost, 0)), 0) AS value,
                   COALESCE(SUM(a.quantity - a.restored_quantity), 0) AS qty,
                   COALESCE(SUM(CASE WHEN a.unit_cost IS NULL
                               THEN a.quantity - a.restored_quantity ELSE 0 END),
                             0) AS unknown_qty,
                   COUNT(*) AS n
            FROM stock_batch_allocations AS a
            JOIN store_sales AS s ON s.sale_id = a.sale_id
            WHERE """ + where,
            params,
        ).fetchone()
    finally:
        conn.close()
    return {
        "value": round(float(row[0]), 2),
        "quantity": _round(row[1]),
        "unknown_cost_qty": _round(row[2]),
        "allocation_count": int(row[3]),
    }
