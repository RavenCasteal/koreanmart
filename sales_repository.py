"""
Stage 9  - Point of Sale data access layer.
Stage 11b - ANY active dish can be sold, ONE dish per sale (no cart).

This is the ONLY module that writes sales data. The UI (pages/pos.py) and the
receipt builder never issue SQL. The POS screen passes the chosen
menu_item_id; preview, confirm and the deduction query are ALL pinned to that
one dish so two dishes' recipes can never be mixed into a single sale.

Backward compatibility: when menu_item_id is omitted, the primary POS dish
(singleton = 1, Regular Ramen) is used, exactly as in Stages 1-11a. That
keeps every older caller and regression test valid. No barcode, QR or SKU
lookup exists anywhere in this module - the cashier picks a dish by name.

How POS and inventory stay in sync
----------------------------------
A confirmed sale creates ONE ordinary stock-out header with reason "Sale" and
one detail line per recipe ingredient. Because vw_stock_movements already
unions stock_out_details, the Dashboard, Inventory, History and Monitoring
screens show the deduction automatically - no changes to their queries and no
duplicate "inventory truth". Balances live only in ingredients.quantity_on_hand.

Accuracy rules
--------------
  1. Preview is a pure READ. It never inserts, updates or deletes.
  2. Confirm re-reads the recipe AND every balance INSIDE a BEGIN IMMEDIATE
     transaction, re-checks sufficiency for ALL lines before the first INSERT,
     then writes everything (sale header, deduction snapshots, stock-out
     header, stock-out lines, balance updates) in ONE commit. Any failure
     rolls the whole thing back: no sale, no deduction, no receipt.
  3. Prices, names, units and deducted quantities are SNAPSHOTS. Editing the
     recipe or changing the price later cannot rewrite a past sale.
  4. A void after commit NEVER deletes history. It marks status = 'voided' and
     writes a compensating stock-in using the ORIGINAL snapshot quantities.
  5. Printing happens OUTSIDE the database transaction (in receipt.py), so a
     printer failure can never roll back a completed sale.

Currency is Philippine peso (PHP / the peso sign) for the whole application.
"""

from __future__ import annotations

import math
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator

import database
import permissions
import usage_repository as usage
from formatting import CURRENCY_CODE, format_quantity

# ----------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------
SALE_REASON = "Sale"
STATUS_COMPLETED = "completed"
STATUS_VOIDED = "voided"

PAYMENT_METHODS: tuple[str, ...] = ("Cash", "Card", "E-wallet")

MAX_PRICE = 1_000_000.0
MAX_AMOUNT = 1_000_000_000.0
MAX_REASON_LENGTH = 200
SALES_HISTORY_LIMIT = 50
EPSILON = 1e-9


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _resolve_path(db_path: Any) -> Any:
    """Use the application database unless a test supplies a temp path."""
    return database.DB_PATH if db_path is None else db_path


@contextmanager
def _read_snapshot(db_path: Any) -> Iterator[sqlite3.Connection]:
    """One consistent, read-only view. Rolls back - never commits."""
    conn = database.get_connection(db_path)
    try:
        conn.execute("BEGIN")
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    """Return plain dicts regardless of the connection's row_factory."""
    columns = [description[0] for description in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _round_money(value: float) -> float:
    return round(float(value), 2)


def _round_balance(value: float) -> float:
    """Remove float dust such as 0.30000000000000004 from stock balances."""
    return max(0.0, round(float(value), 6))


def _resolve_user_id(conn: sqlite3.Connection, user_id: Any) -> int | None:
    """Only a real, active administrator may be recorded as created_by."""
    if user_id is None:
        return None
    try:
        candidate = int(user_id)
    except (TypeError, ValueError):
        return None
    exists = conn.execute(
        "SELECT 1 FROM users WHERE user_id = ? AND is_active = 1", (candidate,)
    ).fetchone()
    return candidate if exists else None


def _username(conn: sqlite3.Connection, user_id: Any) -> str:
    if user_id is None:
        return "—"
    row = conn.execute(
        "SELECT username FROM users WHERE user_id = ?", (int(user_id),)
    ).fetchone()
    return str(row[0]) if row else "—"


def _menu_item(
    conn: sqlite3.Connection, menu_item_id: Any = None
) -> dict[str, Any]:
    """Resolve exactly ONE sellable dish.

    Stage 11b: an explicit id picks that dish. Omitting it falls back to the
    primary POS dish (singleton = 1), preserving Stage 1-11a behaviour.
    """
    if menu_item_id is None:
        row = conn.execute(
            """
            SELECT menu_item_id, name, unit_price, is_active
            FROM menu_items WHERE singleton = 1
            ORDER BY menu_item_id LIMIT 1
            """
        ).fetchone()
    else:
        try:
            key = int(menu_item_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("Please choose a dish before selling.") from exc
        row = conn.execute(
            """
            SELECT menu_item_id, name, unit_price, is_active
            FROM menu_items WHERE menu_item_id = ?
            """,
            (key,),
        ).fetchone()
    if row is None:
        raise ValueError("No active Regular Ramen menu item was found.")
    if int(row["is_active"]) != 1:
        raise ValueError(
            f"'{row['name']}' is not active. Choose another dish to sell."
        )
    return {
        "menu_item_id": int(row["menu_item_id"]),
        "name": str(row["name"]),
        "unit_price": float(row["unit_price"]),
    }


def _sellable_dishes(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Active dishes for the POS dropdown (primary first). Cashier-readable."""
    return _rows(
        conn.execute(
            """
            SELECT menu_item_id, name, unit_price, singleton
            FROM menu_items WHERE is_active = 1
            ORDER BY singleton DESC, name COLLATE NOCASE
            """
        )
    )


def _recipe_lines(
    conn: sqlite3.Connection, menu_item_id: Any = None
) -> list[dict[str, Any]]:
    """Live recipe + live balances for ONE dish, inside the caller's transaction.

    Stage 11b: the dish id comes from _menu_item(), so the preview and the
    commit always use the SAME recipe. Mixing two dishes' lines here would
    over-deduct shared stock. Omitting the id keeps the primary-dish fallback.
    """
    if menu_item_id is None:
        clause, params = "AND m.singleton = 1", ()
    else:
        clause, params = "AND m.menu_item_id = ?", (int(menu_item_id),)
    return _rows(
        conn.execute(
            f"""
            SELECT ri.ingredient_id,
                   ri.quantity_per_serving,
                   i.name,
                   i.unit,
                   i.quantity_on_hand,
                   i.is_active
            FROM recipe_ingredients AS ri
            JOIN ingredients AS i  ON i.ingredient_id = ri.ingredient_id
            JOIN recipes AS r      ON r.recipe_id = ri.recipe_id
            JOIN menu_items AS m   ON m.menu_item_id = r.menu_item_id
            WHERE m.is_active = 1 {clause}
            ORDER BY i.name COLLATE NOCASE
            """,
            params,
        )
    )


def _max_servings(lines: list[dict[str, Any]]) -> int:
    if not lines:
        return 0
    best: int | None = None
    for line in lines:
        per_serving = float(line["quantity_per_serving"])
        on_hand = float(line["quantity_on_hand"])
        if int(line["is_active"]) != 1 or per_serving <= 0:
            return 0
        possible = max(0, math.floor((on_hand + EPSILON) / per_serving))
        best = possible if best is None else min(best, possible)
    return int(best or 0)


def _sales_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT COUNT(*) AS total,
               COALESCE(SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END), 0)
                   AS completed,
               COALESCE(SUM(CASE WHEN status = 'voided' THEN 1 ELSE 0 END), 0)
                   AS voided,
               COALESCE(SUM(CASE WHEN status = 'completed'
                                 THEN total_amount ELSE 0 END), 0) AS gross,
               MAX(transaction_date) AS last_sale
        FROM sales_transactions
        """
    ).fetchone()
    return {
        "total_sales": int(row["total"]),
        "completed_sales": int(row["completed"]),
        "voided_sales": int(row["voided"]),
        "gross_sales": _round_money(row["gross"]),
        "last_sale": row["last_sale"] or "—",
    }


def _next_transaction_number(
    conn: sqlite3.Connection, timestamp: str
) -> str:
    """RR-YYYYMMDD-NNNN, unique per day, safe under BEGIN IMMEDIATE."""
    day = timestamp[:10].replace("-", "")
    prefix = f"RR-{day}-"
    last = conn.execute(
        """
        SELECT MAX(CAST(SUBSTR(transaction_number, ?) AS INTEGER))
        FROM sales_transactions
        WHERE transaction_number LIKE ?
        """,
        (len(prefix) + 1, prefix + "%"),
    ).fetchone()[0]
    sequence = int(last or 0) + 1
    return prefix + str(sequence).zfill(4)


# ----------------------------------------------------------------------
# Validation (all raise ValueError with a friendly message)
# ----------------------------------------------------------------------
def validate_payment_method(value: Any) -> str:
    method = str(value if value is not None else "").strip()
    if method not in PAYMENT_METHODS:
        raise ValueError(
            "Choose a payment method: " + ", ".join(PAYMENT_METHODS) + "."
        )
    return method


def validate_amount(value: Any, label: str = "Amount") -> float:
    text = str(value if value is not None else "").strip()
    if not text:
        raise ValueError(f"{label} is required.")
    # Peso input may include a thousands separator or the peso sign.
    cleaned = text.replace("\u20b1", "").replace(",", "").strip()
    try:
        number = float(cleaned)
    except ValueError as exc:
        raise ValueError(f"{label} must be a number, e.g. 500 or 500.00.") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{label} cannot be negative.")
    if number > MAX_AMOUNT:
        raise ValueError(f"{label} is too large.")
    return _round_money(number)


def validate_unit_price(value: Any) -> float:
    number = validate_amount(value, "Unit price")
    if number <= 0:
        raise ValueError("Unit price must be greater than zero.")
    if number > MAX_PRICE:
        raise ValueError(f"Unit price must be {MAX_PRICE:g} or less.")
    return number


def validate_void_reason(value: Any) -> str:
    reason = str(value if value is not None else "").strip()
    if not reason:
        raise ValueError("A reason for voiding the sale is required.")
    if len(reason) > MAX_REASON_LENGTH:
        raise ValueError(
            f"Reason must be {MAX_REASON_LENGTH} characters or fewer."
        )
    return reason


# ----------------------------------------------------------------------
# Reads
# ----------------------------------------------------------------------
def get_unit_price(
    db_path: Any = None, menu_item_id: Any = None
) -> float:
    """Current price of one dish in PHP (primary dish when id is omitted)."""
    with _read_snapshot(_resolve_path(db_path)) as conn:
        return _round_money(_menu_item(conn, menu_item_id)["unit_price"])


def list_sellable_dishes(db_path: Any = None) -> list[dict[str, Any]]:
    """Active dishes for the POS dropdown.

    A cashier MAY read this: seeing the menu is required to take an order.
    Editing dishes is still manager-only (menu_repository.py). No permission
    guard here on purpose; there is nothing to mutate and no management data.
    """
    with _read_snapshot(_resolve_path(db_path)) as conn:
        return _sellable_dishes(conn)


def set_unit_price(
    value: Any,
    db_path: Any = None,
    actor: Any = None,
    menu_item_id: Any = None,
) -> float:
    """Update ONE dish's price. Never touches sales history or other dishes."""
    permissions.require(actor, permissions.CAP_SET_PRICE)
    price = validate_unit_price(value)
    with database.transaction(_resolve_path(db_path)) as conn:
        menu = _menu_item(conn, menu_item_id)
        cursor = conn.execute(
            "UPDATE menu_items SET unit_price = ? WHERE menu_item_id = ?",
            (price, menu["menu_item_id"]),
        )
        if cursor.rowcount < 1:
            raise ValueError("No active Regular Ramen menu item was found.")
    return price


def get_pos_context(
    db_path: Any = None, menu_item_id: Any = None
) -> dict[str, Any]:
    """Everything the POS screen needs, in one consistent read snapshot."""
    with _read_snapshot(_resolve_path(db_path)) as conn:
        menu = _menu_item(conn, menu_item_id)
        lines = _recipe_lines(conn, menu["menu_item_id"])
        summary = _sales_summary(conn)
        dishes = _sellable_dishes(conn)
    return {
        "menu_item_id": menu["menu_item_id"],
        "menu_item_name": menu["name"],
        "unit_price": _round_money(menu["unit_price"]),
        "currency_code": CURRENCY_CODE,
        "lines": lines,
        "dishes": dishes,
        "max_servings": _max_servings(lines),
        "summary": summary,
    }


def preview_sale(
    servings: Any,
    payment_method: Any,
    amount_tendered: Any,
    db_path: Any = None,
    menu_item_id: Any = None,
) -> dict[str, Any]:
    """READ-ONLY preview: totals, payment and per-ingredient deductions.

    Nothing is written, so cancelling here leaves zero rows everywhere.
    An insufficient result is returned (not raised) so the UI can list the
    shortages and suggest the maximum number of servings.
    """
    count = usage.validate_servings(servings)
    method = validate_payment_method(payment_method)
    tendered = validate_amount(amount_tendered, "Amount tendered")

    context = get_pos_context(db_path, menu_item_id)
    dish_name = str(context["menu_item_name"])
    if context["unit_price"] <= 0:
        raise ValueError(
            f"Set a unit price for {dish_name} before recording a sale."
        )
    if not context["lines"]:
        raise ValueError(
            f"The {dish_name} recipe has no ingredients yet. "
            "Add some on the Recipe screen first."
        )

    unit_price = context["unit_price"]
    total = _round_money(unit_price * count)
    if tendered + EPSILON < total:
        raise ValueError(
            "Amount tendered is less than the total "
            f"({format_peso_short(tendered)} vs {format_peso_short(total)})."
        )
    change = _round_money(tendered - total)

    lines: list[dict[str, Any]] = []
    for raw in context["lines"]:
        per_serving = float(raw["quantity_per_serving"])
        on_hand = float(raw["quantity_on_hand"])
        required = _round_balance(per_serving * count)
        shortfall = _round_balance(max(0.0, required - on_hand - EPSILON))
        active = int(raw["is_active"]) == 1
        lines.append(
            {
                "ingredient_id": int(raw["ingredient_id"]),
                "name": str(raw["name"]),
                "unit": str(raw["unit"]),
                "quantity_per_serving": per_serving,
                "quantity_required": required,
                "quantity_on_hand": on_hand,
                "shortfall": shortfall,
                "is_active": active,
                "sufficient": active and shortfall <= EPSILON,
            }
        )

    shortages = [line for line in lines if not line["sufficient"]]
    return {
        "servings": count,
        "menu_item_id": int(context["menu_item_id"]),
        "menu_item_name": dish_name,
        "unit_price": unit_price,
        "total_amount": total,
        "currency_code": CURRENCY_CODE,
        "payment_method": method,
        "amount_tendered": tendered,
        "change_amount": change,
        "lines": lines,
        "sufficient": not shortages,
        "shortages": shortages,
        "max_servings": context["max_servings"],
    }


def format_peso_short(value: Any) -> str:
    """Short peso text for validation messages (no SQL, no Tkinter)."""
    from formatting import format_money

    return format_money(value)


def get_sales_history(
    status: str = "ALL",
    search: str = "",
    limit: int = SALES_HISTORY_LIMIT,
    db_path: Any = None,
) -> dict[str, Any]:
    """Filtered recent sales plus global totals for the summary cards."""
    kind = str(status or "ALL").upper()
    if kind not in ("ALL", STATUS_COMPLETED.upper(), STATUS_VOIDED.upper()):
        raise ValueError("Status must be All, completed or voided.")

    conditions: list[str] = []
    params: list[Any] = []
    if kind != "ALL":
        conditions.append("s.status = ?")
        params.append(kind.lower())

    keyword = str(search or "").strip()
    if keyword:
        escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conditions.append(
            "("
            "s.transaction_number LIKE ? ESCAPE '\\' OR "
            "s.menu_item_name LIKE ? ESCAPE '\\' OR "
            "s.payment_method LIKE ? ESCAPE '\\' OR "
            "s.transaction_date LIKE ? ESCAPE '\\' OR "
            "COALESCE(s.void_reason, '') LIKE ? ESCAPE '\\'"
            ")"
        )
        params.extend([f"%{escaped}%"] * 5)
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    cap = max(1, min(int(limit), 500))

    with _read_snapshot(_resolve_path(db_path)) as conn:
        summary = _sales_summary(conn)
        filtered = conn.execute(
            f"SELECT COUNT(*) FROM sales_transactions AS s {where}", params
        ).fetchone()[0]
        rows = _rows(
            conn.execute(
                f"""
                SELECT s.sale_id, s.transaction_number, s.transaction_date,
                       s.menu_item_name, s.quantity, s.unit_price,
                       s.total_amount, s.currency_code, s.payment_method,
                       s.amount_tendered, s.change_amount, s.status,
                       s.voided_at, s.void_reason,
                       u.username AS created_by_username
                FROM sales_transactions AS s
                LEFT JOIN users AS u ON u.user_id = s.created_by
                {where}
                ORDER BY s.transaction_date DESC, s.sale_id DESC
                LIMIT ?
                """,
                (*params, cap),
            )
        )
    return {
        "rows": rows,
        "summary": summary,
        "filtered": int(filtered),
        "limit": cap,
    }


def get_sale_detail(sale_id: Any, db_path: Any = None) -> dict[str, Any]:
    """Full immutable sale record - used for receipts, reprints and voids."""
    try:
        key = int(sale_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid sale reference.") from exc

    with _read_snapshot(_resolve_path(db_path)) as conn:
        row = conn.execute(
            """
            SELECT s.*,
                   u.username AS created_by_username,
                   v.username AS voided_by_username
            FROM sales_transactions AS s
            LEFT JOIN users AS u ON u.user_id = s.created_by
            LEFT JOIN users AS v ON v.user_id = s.voided_by
            WHERE s.sale_id = ?
            """,
            (key,),
        ).fetchone()
        if row is None:
            raise ValueError("That sale no longer exists.")
        sale = dict(row)
        lines = _rows(
            conn.execute(
                """
                SELECT ingredient_name, unit, quantity_per_serving,
                       quantity_deducted, balance_before, balance_after
                FROM sale_deductions
                WHERE sale_id = ?
                ORDER BY sale_deduction_id
                """,
                (key,),
            )
        )
        reversal = conn.execute(
            """
            SELECT stock_in_id FROM stock_in_transactions
            WHERE reference_no = ?
            ORDER BY stock_in_id DESC LIMIT 1
            """,
            ("VOID " + str(sale["transaction_number"]),),
        ).fetchone()

    sale["cashier"] = sale.pop("created_by_username") or "—"
    sale["voided_by_username"] = sale["voided_by_username"] or "—"
    sale["lines"] = lines
    sale["reversal_id"] = int(reversal[0]) if reversal else None
    return sale


# ----------------------------------------------------------------------
# Writes - one database transaction per business transaction
# ----------------------------------------------------------------------
def confirm_sale(
    servings: Any,
    payment_method: Any,
    amount_tendered: Any,
    user_id: Any = None,
    db_path: Any = None,
    actor: Any = None,
    menu_item_id: Any = None,
) -> dict[str, Any]:
    """Commit a sale: write money, deduction snapshots, stock-out and balances.

    The sufficiency check is repeated INSIDE BEGIN IMMEDIATE so another window
    cannot consume stock between the preview and the commit. Every line is
    validated before the first INSERT. On any error the whole transaction is
    rolled back: no sale row, no movement, no balance change, no receipt.

    Stage 10: recording a sale is allowed for BOTH roles; it is the one write
    a cashier may perform. actor is checked anyway so a forged/unknown role
    cannot reach it.

    Stage 11b: menu_item_id selects the dish. The dish is re-resolved INSIDE
    BEGIN IMMEDIATE, so a manager deactivating or repricing a dish between the
    preview and the commit cannot make the sale charge or deduct the wrong
    recipe. The dish name and unit price are then written as snapshots.
    """
    permissions.require(actor, permissions.CAP_RECORD_SALE)
    count = usage.validate_servings(servings)
    method = validate_payment_method(payment_method)
    tendered = validate_amount(amount_tendered, "Amount tendered")
    timestamp = database.local_timestamp()

    with database.transaction(_resolve_path(db_path)) as conn:
        menu = _menu_item(conn, menu_item_id)
        dish_name = str(menu["name"])
        unit_price = _round_money(menu["unit_price"])
        if unit_price <= 0:
            raise ValueError(
                f"Set a unit price for {dish_name} before recording a sale."
            )
        recipe_lines = _recipe_lines(conn, menu["menu_item_id"])
        if not recipe_lines:
            raise ValueError(
                f"The {dish_name} recipe has no ingredients yet."
            )

        total = _round_money(unit_price * count)
        if tendered + EPSILON < total:
            raise ValueError(
                "Amount tendered is less than the total. "
                "The unit price may have changed - check the preview again."
            )
        change = _round_money(tendered - total)

        # 1 - Validate ALL lines before any INSERT.
        deductions: list[dict[str, Any]] = []
        for raw in recipe_lines:
            per_serving = float(raw["quantity_per_serving"])
            on_hand = float(raw["quantity_on_hand"])
            name = str(raw["name"])
            unit = str(raw["unit"])
            if int(raw["is_active"]) != 1:
                raise ValueError(
                    f"{name} is no longer active. Update the recipe first."
                )
            required = _round_balance(per_serving * count)
            if required > on_hand + EPSILON:
                raise ValueError(
                    f"Not enough {name}: "
                    f"{format_quantity(on_hand, unit)} on hand, "
                    f"{format_quantity(required, unit)} required "
                    f"for {count} serving(s)."
                )
            deductions.append(
                {
                    "ingredient_id": int(raw["ingredient_id"]),
                    "name": name,
                    "unit": unit,
                    "quantity_per_serving": per_serving,
                    "quantity_deducted": required,
                    "balance_before": on_hand,
                    "balance_after": _round_balance(on_hand - required),
                }
            )

        created_by = _resolve_user_id(conn, user_id)
        cashier = _username(conn, created_by)
        transaction_number = _next_transaction_number(conn, timestamp)

        # 2 - Stock-out header + details. vw_stock_movements picks these up.
        out_header = conn.execute(
            """
            INSERT INTO stock_out_transactions
                (transaction_date, reason, notes, created_by)
            VALUES (?, ?, ?, ?)
            """,
            (
                timestamp,
                SALE_REASON,
                f"POS sale {transaction_number}",
                created_by,
            ),
        )
        stock_out_id = int(out_header.lastrowid)
        for line in deductions:
            conn.execute(
                """
                INSERT INTO stock_out_details
                    (stock_out_id, ingredient_id, quantity, unit, notes)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    stock_out_id,
                    line["ingredient_id"],
                    line["quantity_deducted"],
                    line["unit"],
                    f"POS sale {transaction_number}",
                ),
            )
            conn.execute(
                """
                UPDATE ingredients
                SET quantity_on_hand = ?, updated_at = ?
                WHERE ingredient_id = ?
                """,
                (line["balance_after"], timestamp, line["ingredient_id"]),
            )

        # 3 - Sale header (money snapshots) + immutable deduction snapshots.
        sale_header = conn.execute(
            """
            INSERT INTO sales_transactions
                (transaction_number, transaction_date, menu_item_name,
                 quantity, unit_price, total_amount, currency_code,
                 payment_method, amount_tendered, change_amount, status,
                 stock_out_id, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                transaction_number,
                timestamp,
                menu["name"],
                count,
                unit_price,
                total,
                CURRENCY_CODE,
                method,
                tendered,
                change,
                STATUS_COMPLETED,
                stock_out_id,
                created_by,
            ),
        )
        sale_id = int(sale_header.lastrowid)
        for line in deductions:
            conn.execute(
                """
                INSERT INTO sale_deductions
                    (sale_id, ingredient_id, ingredient_name, unit,
                     quantity_per_serving, quantity_deducted,
                     balance_before, balance_after)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sale_id,
                    line["ingredient_id"],
                    line["name"],
                    line["unit"],
                    line["quantity_per_serving"],
                    line["quantity_deducted"],
                    line["balance_before"],
                    line["balance_after"],
                ),
            )

    return {
        "sale_id": sale_id,
        "transaction_number": transaction_number,
        "transaction_date": timestamp,
        "menu_item_id": int(menu["menu_item_id"]),
        "menu_item_name": dish_name,
        "quantity": count,
        "unit_price": unit_price,
        "total_amount": total,
        "currency_code": CURRENCY_CODE,
        "payment_method": method,
        "amount_tendered": tendered,
        "change_amount": change,
        "status": STATUS_COMPLETED,
        "cashier": cashier,
        "stock_out_id": stock_out_id,
        "lines": deductions,
    }


def void_sale(
    sale_id: Any,
    reason: Any,
    user_id: Any = None,
    db_path: Any = None,
    actor: Any = None,
) -> dict[str, Any]:
    """Cancel an ALREADY CONFIRMED sale without deleting any history.

    The sale becomes status = 'voided' and a compensating stock-in restores
    exactly the ORIGINAL snapshotted quantities. Using the snapshots (not the
    live recipe) is essential when the recipe changed after the sale.

    Stage 10: voiding is MANAGER ONLY. A cashier cannot reverse a sale (and
    therefore cannot restore stock) on their own.
    """
    permissions.require(actor, permissions.CAP_VOID_SALE)
    clean_reason = validate_void_reason(reason)
    try:
        key = int(sale_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid sale reference.") from exc
    timestamp = database.local_timestamp()

    with database.transaction(_resolve_path(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM sales_transactions WHERE sale_id = ?", (key,)
        ).fetchone()
        if row is None:
            raise ValueError("That sale no longer exists.")
        if str(row["status"]) == STATUS_VOIDED:
            raise ValueError("This sale is already voided.")
        transaction_number = str(row["transaction_number"])

        lines = _rows(
            conn.execute(
                """
                SELECT ingredient_id, ingredient_name, unit,
                       quantity_deducted, balance_before, balance_after
                FROM sale_deductions WHERE sale_id = ?
                ORDER BY sale_deduction_id
                """,
                (key,),
            )
        )
        if not lines:
            raise ValueError("This sale has no deduction lines to reverse.")

        # Validate ALL lines before the first INSERT.
        reversals: list[dict[str, Any]] = []
        for line in lines:
            current = conn.execute(
                """
                SELECT quantity_on_hand FROM ingredients
                WHERE ingredient_id = ?
                """,
                (line["ingredient_id"],),
            ).fetchone()
            if current is None:
                raise ValueError(
                    f"{line['ingredient_name']} is missing from the catalog; "
                    "the sale cannot be reversed."
                )
            reversals.append(
                {
                    **line,
                    "quantity_on_hand": float(current[0]),
                    "restored_balance": _round_balance(
                        float(current[0]) + float(line["quantity_deducted"])
                    ),
                }
            )

        voided_by = _resolve_user_id(conn, user_id)
        reference = "VOID " + transaction_number
        in_header = conn.execute(
            """
            INSERT INTO stock_in_transactions
                (transaction_date, reference_no, notes, created_by)
            VALUES (?, ?, ?, ?)
            """,
            (
                timestamp,
                reference,
                f"Reversal of voided sale {transaction_number}: {clean_reason}",
                voided_by,
            ),
        )
        stock_in_id = int(in_header.lastrowid)
        for line in reversals:
            conn.execute(
                """
                INSERT INTO stock_in_details
                    (stock_in_id, ingredient_id, quantity, unit, notes)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    stock_in_id,
                    line["ingredient_id"],
                    line["quantity_deducted"],
                    line["unit"],
                    f"Void reversal of {transaction_number}",
                ),
            )
            conn.execute(
                """
                UPDATE ingredients
                SET quantity_on_hand = ?, updated_at = ?
                WHERE ingredient_id = ?
                """,
                (
                    line["restored_balance"],
                    timestamp,
                    line["ingredient_id"],
                ),
            )

        conn.execute(
            """
            UPDATE sales_transactions
            SET status = ?, voided_at = ?, voided_by = ?, void_reason = ?
            WHERE sale_id = ?
            """,
            (STATUS_VOIDED, timestamp, voided_by, clean_reason, key),
        )
        cashier = _username(conn, row["created_by"])
        voided_by_username = _username(conn, voided_by)

    return {
        "sale_id": key,
        "transaction_number": transaction_number,
        "transaction_date": str(row["transaction_date"]),
        "menu_item_name": str(row["menu_item_name"]),
        "quantity": int(row["quantity"]),
        "unit_price": _round_money(row["unit_price"]),
        "total_amount": _round_money(row["total_amount"]),
        "currency_code": str(row["currency_code"]),
        "payment_method": str(row["payment_method"]),
        "amount_tendered": _round_money(row["amount_tendered"]),
        "change_amount": _round_money(row["change_amount"]),
        "status": STATUS_VOIDED,
        "cashier": cashier,
        "voided_at": timestamp,
        "voided_by": voided_by_username,
        "void_reason": clean_reason,
        "stock_in_id": stock_in_id,
        "lines": reversals,
    }
