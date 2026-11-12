-- ============================================================
-- Ramen Inventory System - SQLite schema
-- Python 3.13 / built-in sqlite3
-- ============================================================
-- Conventions:
--   * All primary keys are INTEGER PRIMARY KEY AUTOINCREMENT.
--   * All dates are ISO-8601 TEXT: "YYYY-MM-DD" for expiry dates,
--     "YYYY-MM-DD HH:MM:SS" (local time) for transactions.
--     SQLite has no date type, so TEXT sorts correctly as text.
--   * PRAGMA foreign_keys = ON is enabled per connection in database.py.
--     SQLite does NOT enforce foreign keys by default.
--   * Recipe amounts and physical stock are stored in separate tables.
-- ============================================================


-- ------------------------------------------------------------
-- User accounts (Stage 10 added the role)
-- password_hash format: pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>
-- The plain-text password is never stored.
--
-- role = 'manager' : sees all screens, manages stock / recipe / users.
-- role = 'cashier' : sees ONLY the Point of Sale screen and receipts.
-- The default is deliberately 'manager' so a pre-Stage-10 database that
-- gains this column keeps its existing administrator fully functional.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    user_id               INTEGER PRIMARY KEY AUTOINCREMENT,
    username              TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    password_hash         TEXT    NOT NULL,
    full_name             TEXT    NOT NULL DEFAULT 'Administrator',
    role                  TEXT    NOT NULL DEFAULT 'manager'
                          CHECK (role IN ('manager', 'cashier')),
    is_active             INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    must_change_password  INTEGER NOT NULL DEFAULT 0
                          CHECK (must_change_password IN (0, 1)),
    created_at            TEXT    NOT NULL,
    last_login_at         TEXT
);


-- ------------------------------------------------------------
-- Menu items (Stage 11a: MORE THAN ONE DISH is now allowed).
--
-- The original capstone rule was "one menu item only". Stage 11a relaxes it
-- so a manager can add new sellable dishes, each with its OWN recipe and
-- OWN price. Physical stock stays shared in ingredients.quantity_on_hand.
--
-- "singleton" now means "the PRIMARY dish that the current POS sells".
-- Stage 11a deliberately keeps the POS unchanged, so it needs exactly one
-- unambiguous dish to charge and deduct for. The PARTIAL unique index below
-- allows any number of dishes with singleton = 0 but only ONE with = 1.
-- Stage 11b will add a POS dish picker and retire this primary flag.
--
-- unit_price is in Philippine peso (PHP); 150.00 is just a starting value.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS menu_items (
    menu_item_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    description   TEXT,
    is_active     INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    -- DEFAULT 1 keeps the original one-dish build working. New dishes are
    -- always inserted by menu_repository.add_dish(), which passes 0 explicitly.
    singleton     INTEGER NOT NULL DEFAULT 1 CHECK (singleton IN (0, 1)),
    unit_price    REAL    NOT NULL DEFAULT 150.0,
    created_at    TEXT    NOT NULL
);

-- At most ONE primary (POS) dish; ordinary dishes all have singleton = 0.
CREATE UNIQUE INDEX IF NOT EXISTS ux_menu_items_primary
    ON menu_items (singleton) WHERE singleton = 1;


-- ------------------------------------------------------------
-- One recipe (bill of materials) per menu item. menu_item_id is UNIQUE,
-- so every dish has its own per-serving ingredient lines.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS recipes (
    recipe_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    menu_item_id  INTEGER NOT NULL UNIQUE
                  REFERENCES menu_items (menu_item_id) ON DELETE RESTRICT,
    notes         TEXT,
    updated_at    TEXT
);


-- ------------------------------------------------------------
-- Ingredient catalog + CURRENT STOCK BALANCE.
-- quantity_on_hand is a cached balance; it may only be changed by a
-- stock-in / stock-out transaction (see Stage 5), never by a form.
-- Deleting an ingredient is a SOFT delete (is_active = 0) so that
-- historical transactions keep a valid foreign key.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ingredients (
    ingredient_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT    NOT NULL,
    unit              TEXT    NOT NULL CHECK (length(trim(unit)) > 0),
    quantity_on_hand  REAL    NOT NULL DEFAULT 0 CHECK (quantity_on_hand >= 0),
    minimum_stock     REAL    NOT NULL DEFAULT 0 CHECK (minimum_stock >= 0),
    expiration_date   TEXT,
    is_active         INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL,
    CHECK (expiration_date IS NULL
           OR expiration_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')
);

-- Duplicate names are prevented among ACTIVE ingredients only.
-- A soft-deleted name can be reused later without breaking history.
CREATE UNIQUE INDEX IF NOT EXISTS ux_ingredients_active_name
    ON ingredients (name COLLATE NOCASE) WHERE is_active = 1;

CREATE INDEX IF NOT EXISTS ix_ingredients_expiration
    ON ingredients (expiration_date);


-- ------------------------------------------------------------
-- RECIPE definition: amount needed for ONE serving of ONE dish.
-- Stage 11a: recipe_id belongs to one dish, so several dishes can each have
-- their own bill of materials while SHARING the same physical stock table.
-- The unit comes from ingredients.unit (single source of truth),
-- so recipe and stock can never disagree about measurement units.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS recipe_ingredients (
    recipe_ingredient_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe_id             INTEGER NOT NULL
                          REFERENCES recipes (recipe_id) ON DELETE CASCADE,
    ingredient_id         INTEGER NOT NULL
                          REFERENCES ingredients (ingredient_id) ON DELETE RESTRICT,
    quantity_per_serving  REAL    NOT NULL CHECK (quantity_per_serving > 0),
    UNIQUE (recipe_id, ingredient_id)
);


-- ------------------------------------------------------------
-- Stock-IN (deliveries): header + detail lines.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stock_in_transactions (
    stock_in_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_date  TEXT    NOT NULL,
    reference_no      TEXT,
    notes             TEXT,
    created_by        INTEGER REFERENCES users (user_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS stock_in_details (
    stock_in_detail_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_in_id         INTEGER NOT NULL
                        REFERENCES stock_in_transactions (stock_in_id) ON DELETE CASCADE,
    ingredient_id       INTEGER NOT NULL
                        REFERENCES ingredients (ingredient_id) ON DELETE RESTRICT,
    quantity            REAL    NOT NULL CHECK (quantity > 0),
    unit                TEXT    NOT NULL,
    expiration_date     TEXT,
    notes               TEXT
);

CREATE INDEX IF NOT EXISTS ix_stock_in_details_ingredient
    ON stock_in_details (ingredient_id);


-- ------------------------------------------------------------
-- Stock-OUT (consumption / removal): header + detail lines.
-- "reason" on the header answers "why was stock removed?"
-- e.g. Consumption, Expired, Damaged, Manual correction.
-- Stage 9 also writes reason = 'Sale', but ONLY from sales_repository;
-- never from the manual stock-out form, so every Sale has a sale record.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stock_out_transactions (
    stock_out_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_date  TEXT    NOT NULL,
    reason            TEXT    NOT NULL CHECK (length(trim(reason)) > 0),
    notes             TEXT,
    created_by        INTEGER REFERENCES users (user_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS stock_out_details (
    stock_out_detail_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_out_id         INTEGER NOT NULL
                         REFERENCES stock_out_transactions (stock_out_id) ON DELETE CASCADE,
    ingredient_id        INTEGER NOT NULL
                         REFERENCES ingredients (ingredient_id) ON DELETE RESTRICT,
    quantity             REAL    NOT NULL CHECK (quantity > 0),
    unit                 TEXT    NOT NULL,
    notes                TEXT
);

CREATE INDEX IF NOT EXISTS ix_stock_out_details_ingredient
    ON stock_out_details (ingredient_id);


-- ------------------------------------------------------------
-- Stage 9 - POINT OF SALE.
-- Stage 11a still sells only the PRIMARY dish (singleton = 1); Stage 11b
-- will add a dish picker. menu_item_name is a snapshot on purpose, so a
-- renamed or deactivated dish never rewrites an old receipt.
--
-- A confirmed sale writes ONE ordinary stock-out header (reason = 'Sale')
-- with one detail line per recipe ingredient. That is what makes the
-- existing vw_stock_movements, Dashboard, History and Monitoring screens
-- show the deduction automatically, with no changes to their queries.
--
-- Money columns are SNAPSHOTS: changing the recipe or the unit price later
-- must never rewrite what a past sale charged or deducted.
-- Currency is Philippine peso (PHP). No other currency is used.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sales_transactions (
    sale_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_number  TEXT    NOT NULL UNIQUE,
    transaction_date    TEXT    NOT NULL,
    menu_item_name      TEXT    NOT NULL,
    quantity            INTEGER NOT NULL CHECK (quantity > 0),
    unit_price          REAL    NOT NULL CHECK (unit_price >= 0),
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
);

CREATE INDEX IF NOT EXISTS ix_sales_transaction_date
    ON sales_transactions (transaction_date);

-- Immutable per-ingredient audit trail. Copies are intentional: a recipe
-- edit, a rename or a unit change must not alter a completed sale.
CREATE TABLE IF NOT EXISTS sale_deductions (
    sale_deduction_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    sale_id              INTEGER NOT NULL
                         REFERENCES sales_transactions (sale_id) ON DELETE CASCADE,
    ingredient_id        INTEGER NOT NULL
                         REFERENCES ingredients (ingredient_id) ON DELETE RESTRICT,
    ingredient_name      TEXT    NOT NULL,
    unit                 TEXT    NOT NULL,
    quantity_per_serving REAL    NOT NULL CHECK (quantity_per_serving > 0),
    quantity_deducted    REAL    NOT NULL CHECK (quantity_deducted > 0),
    balance_before       REAL    NOT NULL CHECK (balance_before >= 0),
    balance_after        REAL    NOT NULL CHECK (balance_after >= 0)
);

CREATE INDEX IF NOT EXISTS ix_sale_deductions_sale
    ON sale_deductions (sale_id);


-- ------------------------------------------------------------
-- Unified movement history (used by the dashboard and history page).
-- Stock-out quantities are negative so a running balance is easy to read.
-- NOTE: CREATE VIEW IF NOT EXISTS does not update an existing view;
-- if this definition changes later, DROP VIEW IF EXISTS first.
-- ------------------------------------------------------------
CREATE VIEW IF NOT EXISTS vw_stock_movements AS
SELECT
    'IN'                        AS movement_type,
    h.stock_in_id               AS transaction_id,
    d.stock_in_detail_id        AS detail_id,
    h.transaction_date          AS transaction_date,
    d.ingredient_id             AS ingredient_id,
    i.name                      AS ingredient_name,
    d.unit                      AS unit,
    d.quantity                  AS quantity,
    NULL                        AS reason,
    COALESCE(h.notes, '')       AS notes,
    h.created_by                AS created_by
FROM stock_in_transactions AS h
JOIN stock_in_details AS d  ON d.stock_in_id = h.stock_in_id
JOIN ingredients AS i       ON i.ingredient_id = d.ingredient_id
UNION ALL
SELECT
    'OUT'                       AS movement_type,
    h.stock_out_id              AS transaction_id,
    d.stock_out_detail_id       AS detail_id,
    h.transaction_date          AS transaction_date,
    d.ingredient_id             AS ingredient_id,
    i.name                      AS ingredient_name,
    d.unit                      AS unit,
    -d.quantity                 AS quantity,
    h.reason                    AS reason,
    COALESCE(h.notes, '')       AS notes,
    h.created_by                AS created_by
FROM stock_out_transactions AS h
JOIN stock_out_details AS d  ON d.stock_out_id = h.stock_out_id
JOIN ingredients AS i        ON i.ingredient_id = d.ingredient_id;
