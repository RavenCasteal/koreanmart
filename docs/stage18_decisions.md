# Stage 18 - inventory database improvements: decisions and limitations

Scope: batch / lot inventory, suppliers, purchase-cost snapshots, reconciliation,
WAL-safe backup/restore, discount data guards, and reduced customer ID exposure.
No existing sale, receipt, stock-in reference, note, user account or password
was rewritten. `PRAGMA user_version` stays at **4**; all Stage 18 changes are
additive and guarded by `app_meta.stage18_schema = 18`.

## Schema added (all `IF NOT EXISTS` / guarded `ALTER TABLE`)

| Table | Purpose |
| --- | --- |
| `suppliers` | `supplier_id`, `name`, `contact_info`, `notes`, `is_active`, `created_at`. Unique active name (case-insensitive partial index). |
| `stock_batches` | One row per receipt: `ingredient_id`, optional `stock_in_detail_id`, optional `supplier_id`, `received_qty`, `remaining_qty`, optional `expiration_date`, optional `unit_cost`, optional `received_at`, `is_opening`, `notes`, timestamps. CHECKs prevent negative balances and negative costs. |
| `stock_batch_allocations` | One row per (stock-out detail line, batch): `quantity`, `unit_cost` snapshot, `sale_id` (soft reference for legacy DBs), `restored_quantity`, `restored_at`. |

Additive columns: `stock_in_transactions.supplier_id`, `stock_in_details.unit_cost`,
`stock_in_details.batch_id`. Old rows keep NULL defaults.

Two `BEFORE INSERT/UPDATE` triggers on `store_sales` enforce discount validity at
the database level (allowed types `'' / 'senior' / 'pwd'`, rate `0..1`, non-negative
discount not exceeding subtotal, name+ID present only on a discounted sale).
SQLite cannot add a CHECK with `ALTER TABLE`, and rebuilding `store_sales` would
break its child FK and the "preserve old sales" rule, so triggers are the additive
solution. **Existing rows are never validated away.** The UPDATE trigger fires
only when a discount column actually changes, so voiding an old sale still works.
`products.discount_constraint_anomalies()` lists pre-existing outliers read-only;
it repairs nothing.

## Migration behaviour

* `products.ensure_schema()` (called on every launch from `main.py`) now also runs
  `batches.ensure_schema()` + `batches.backfill_unbatched()` inside the SAME
  `BEGIN IMMEDIATE` transaction. Idempotent.
* Every active product with on-hand stock and **no batch at all** receives exactly
  ONE opening batch whose `remaining_qty` equals `quantity_on_hand`. Its expiry,
  unit cost, supplier and `received_at` are **NULL (unknown)** - never guessed.
* `products.bootstrap_catalog()` and `products.add_product()` also create that
  opening batch and link it back to the original stock-in detail line.
* `batches.ensure_write_ready()` runs at the start of every stock / sale / void
  write transaction, so a balance written by legacy code self-heals into the
  batch ledger instead of drifting.
* Historical `stock_in_details` rows (including their old expiry dates and
  reference numbers) are left untouched. Back-projecting per-delivery expiry onto
  today's remaining balance would require guessing which delivery each surviving
  unit came from, so it was deliberately NOT done.

## FEFO allocation and voids

* Order: batches with a known expiry first (earliest date), then batches with
  **no** expiry (oldest `received_at` first; NULL receipt date sorts first),
  then `batch_id` as a deterministic tie-breaker.
* `batches.allocate()` checks total availability BEFORE writing anything. A short
  batch raises `ValueError` and the caller's whole transaction rolls back -
  header, all detail lines and all earlier allocations included.
* Product `quantity_on_hand` and batch `remaining_qty` are updated in the same
  transaction. `batches.reconcile()` checks both product-vs-batch totals and
  batch consumed-vs-allocation totals. It is **read-only** and never repairs.
* Voids restore the **exact** batch ids and quantities via `restored_quantity`.
  Pre-batch legacy sales have no allocations; their units return as an
  unknown-provenance opening batch (NULL expiry/cost) so the ledger stays
  consistent. Restoring twice is a no-op.

## Money representation - DEFERRED (owner decision)

**Not converted to integer cents.** Assessment: the project stores every monetary
value as SQLite `REAL` throughout `store_sales`, `store_sale_items`, `receipt.py`,
`sales_report.py`, Z-reads, CSV exports and all Stage 15 discount snapshots, with
`formatting.format_money()` as the single display source. A cents migration would
require rewriting 814 regression tests plus every immutable historical snapshot,
and a rounding rule would be needed for the 20% Senior/PWD discount on amounts
that are not an exact number of centavos - which risks changing a historical
total. That conflicts with the hard requirement "migrate without changing
historical totals" and with "make only changes needed".

If exact money is wanted, it should be a **dedicated, separately verified
migration stage** with an explicit rounding policy, a historical-total
equivalence report, and an owner sign-off. New Stage 18 cost columns therefore
also use `REAL` with `CHECK (unit_cost IS NULL OR unit_cost >= 0)` and 2-decimal
normalisation at entry, matching the rest of the project.

## Customer ID exposure - owner decisions still open

* Full Senior/PWD ID numbers remain on the `store_sales` row and the **printed
  receipt**, because those are the discount evidence.
* They are no longer read into `daily_summary()`, Z-reads, CSV exports or the
  report transactions list. The 13th CSV column is now `ID on file` with
  `Yes` / `-`, preserving the established 13-column layout.
* **No retention period, purge rule, pseudonymisation scheme, access log or
  authorised-viewer list has been invented.** The project owner must decide:
  1. how long name + ID are kept after a sale, and who may read them;
  2. whether old receipts (archived `.txt` files) should be purged on a schedule;
  3. whether IDs should be masked on reprints while keeping the original proof.
  Until then nothing is deleted. `products.discount_constraint_anomalies()` is
  the read-only starting point for reviewing existing data.

## Backup / restore

* `backups.create_backup()` uses SQLite's **online backup API** (`Connection.backup`)
  on a `query_only` source connection, so committed WAL frames are included and
  no raw file copy is used. It writes to a temp file, verifies it
  (`integrity_check`, table count, `foreign_key_check`, sha256), then renames.
* Safe to run while the app is open. Backups live in `backups/` (not shipped).
* `backups.restore_backup()` verifies the source, takes a `pre-restore-*` safety
  backup first, checkpoints WAL, restores via the same API and re-checks
  integrity. The destination file is never deleted or swapped.
* **Operational rule:** close the app (and any other connection) before restoring.
  The code fails with a friendly "is the app still open?" error on a lock rather
  than gambling. Close and relaunch after a successful restore.
* The manager-only Database tools dialog (Inventory screen) exposes create /
  list / restore; restore requires an explicit confirmation tick box.

## UI added (minimal)

* Stock-in form: optional supplier picker + optional `Cost/unit` per delivery
  line (blank = unknown, never 0). Existing reference number and notes unchanged.
* Inventory toolbar: manager-only **Database tools** dialog with supplier
  management, the batch ledger, reconciliation, and backup/restore.
* No new sidebar page: the 12-page registry and role-based page lists are pinned
  by the Stage 9-13 regression suite and this stage is not a navigation redesign.

## Limitations / still open

1. Real USB barcode scanner and ESC/POS thermal printer remain untested hardware
   (unchanged from Stage 17).
2. `stock_batches` tracks received quantity and remaining quantity, not serial
   numbers, lot codes or bin locations.
3. Historical expiry/cost provenance for pre-Stage-18 stock is unknown by design;
   back-projecting it is an owner decision.
4. `cost_of_goods_sold()` and `inventory_valuation()` are **data helpers only** -
   no COGS/valuation report screen was added, per "don't add unrelated reporting
   features". Unknown-cost quantities are reported separately, never valued at 0.
5. Restoring a backup while the app is open is discouraged but not technically
   blocked; the app must be relaunched afterwards to clear any cached state.
6. The public GitHub repo still publishes the live DB, `.venv` and receipts
   (earlier owner choice "Everything"). Adding `.gitignore` / rotating the PAT
   remains an owner decision.
