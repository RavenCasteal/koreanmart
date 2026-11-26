# COOLITS Korean Mart — User Guide

A practical guide to running the store on a day-to-day basis. The app is
Windows-only, offline, and keeps everything in one local SQLite database.
No internet connection is needed to sell.

---

## 1. Getting in

1. Double-click `run.bat` (or the **COOLITS Korean Mart** shortcut on the
   Desktop / in the Start Menu).
2. The login screen shows the store logo. Sign in with your username and
   password.
3. On first login you are forced to change the password. Do it. The default
   manager password is written in `database.py` and is public in the
   repository, so it must be changed before real use.

There are two roles:

| Role | What they can see and do |
|---|---|
| **Manager** | All 12 screens: dashboard, ingredients, products, menu, recipe, calculator, Point of Sale, sales reports, inventory, history, monitoring, users. Can set prices, void sales, export reports, manage accounts and databases. |
| **Cashier** | **Point of Sale only.** Can ring up sales (including discounted ones), view and reprint receipts. Cannot see prices, void sales, access reports or accounts. |

If a cashier logs in and only sees one button on the sidebar, that is correct.

---

## 2. The screens at a glance

| Screen | Purpose |
|---|---|
| **Dashboard** | Live overview of sales, stock and alerts. Read-only. |
| **Ingredients** | Base stock items. For the convenience-store setup you normally work in Products instead. |
| **Products** | The scan-to-sell catalog: barcodes, categories, PHP prices. Manager only. |
| **Menu / Recipe / Calculator** | Legacy dish-and-recipe tools retained from the original ramen build. Not needed for selling packaged products. |
| **Point of Sale** | Scan products, build a basket, take payment, print/archive a receipt. |
| **Sales Reports** | Daily totals, Z-read, top products, CSV export. Manager only. |
| **Inventory** | Record stock-in / stock-out, see live balances, and open the Database Tools dialog. |
| **History** | Search every recorded stock movement. Read-only. |
| **Monitoring** | Low-stock, out-of-stock and expiration alerts. |
| **Users** | Add accounts, reset passwords, deactivate/reactivate. Manager only. |

---

## 3. Setting up a new store (fresh start)

Do this once, on the first day.

### Step 1 — Secure the accounts
- Log in as `admin`, change the password when prompted.
- Go to **Users** and create one account per cashier. Give each a strong
  password. Cashiers see only the POS.
- The last active manager cannot be deactivated, and you cannot deactivate
  your own account.

### Step 2 — Add products
Go to **Products → Add product** for each item. Fill in:

| Field | What to enter |
|---|---|
| Product name | e.g. `Coca-Cola Original 500 ml` |
| Barcode | 6–14 digits, numbers only. Use the barcode on the package, or your own printed label. Must be unique. |
| Category | Pick from the list (Food, Drinks, Sweets, Snacks, Household, etc.) |
| Unit | `piece`, `pack`, `bottle`, … |
| Selling price | Philippine peso, e.g. `35.00` |
| Minimum stock | The level at which you want a low-stock alert, e.g. `10` |
| Expiration date | Optional, `YYYY-MM-DD`. Leave blank for non-perishables. |
| Opening stock | How many you actually have on hand now. |

**Opening stock matters:** entering a number here automatically creates a real
stock-in movement and a batch, so the product is immediately sellable. You do
not need a separate inventory step for the initial count.

### Step 3 — Start selling
Scan a barcode on the **Point of Sale** screen. That's it — no other setup is
required for the basics.

### Optional — Printer
By default receipts are archived silently to `receipts/` and nothing is sent to
a printer. When a thermal printer is connected, use the printer button on the
POS screen to choose the device and switch to ESC/POS mode. See section 7.

---

## 4. Daily selling (Point of Sale)

The cashier screen is deliberately compact and keyboard/scanner-driven.

1. **Scan** the product barcode. The scanner behaves like a keyboard: it types
   the digits and presses Enter. You can also type the barcode and press Enter.
   Each scan adds one unit to the basket.
2. The basket shows each line, its price, and the running **TOTAL**.
3. If the customer is a senior citizen or PWD, open the discount section,
   choose the type, and enter the customer's **name and ID number**. Both are
   required. A single **20% discount** is applied to the whole sale. It cannot
   be combined with anything else.
4. Use the built-in calculator if you want to check quantities without
   changing anything — it never deducts stock.
5. Enter the amount of cash handed over, or tap a **Quick cash** button
   (₱20, ₱50, ₱100, ₱200, ₱500, ₱1000). Live **CHANGE** updates as you type.
6. Press **Confirm sale**. The app checks stock one final time, saves the sale
   and deducts stock in a single atomic operation, then archives and prints the
   receipt. A printer failure can never reverse or duplicate a paid sale.
7. The receipt dialog lets you view or reprint the receipt.

**Important:** Confirm is not blocked by an insufficient cash amount — the app
lets you confirm and shows the change (which may be negative) so the cashier
can correct the tender. Train staff to check the CHANGE figure before
confirming.

### Voiding a sale
Manager only. Open the sale's receipt and choose Void. You must give a reason.
The original sale stays in the records with a voided status, and the stock is
returned. Nothing is ever deleted.

---

## 5. Stock, suppliers and expiry

### Recording stock
Use **Inventory** for all stock changes after the initial setup:

- **Stock-in** — goods received from a supplier. Enter the product, quantity,
  and optionally supplier, unit cost, and expiration date.
- **Stock-out** — goods used, damaged, or removed. Enter the reason.

Every movement appears in **History** and cannot be silently erased.

### Batches and FEFO
Each stock-in creates a batch with its expiry date. When a sale happens, stock
is deducted **FEFO** (first-expiry-first-out): the earliest-expiring batch is
sold first, and batches with no expiry are used last. This reduces spoilage
automatically.

### Alerts
**Monitoring** flags products that are low on stock, out of stock, or nearing
expiration. **Dashboard** shows a compact version of the same warnings.

### Database Tools (Inventory → Database Tools)
Manager only. Lets you:
- Add **suppliers**.
- View batches and **reconcile** them against stock on hand (read-only check).
- Create a **verified backup** of the live database.
- **Restore** a backup. Restore is gated behind an explicit confirmation tick
  box because it overwrites live data; after a restore, close and relaunch the
  app.

---

## 6. Closing the day (reports and Z-read)

Manager only, under **Sales Reports**:

- Pick a date with the day arrows or **Today**.
- Review net sales, payment mix, discounts, and top products.
- **Print / save Z-read** — archives a daily closing summary.
- **Export CSV** — writes a transaction file to `reports/` for spreadsheet use.

The full customer ID is **never** shown in reports or CSV. Reports only show
`ID on file: Yes/-`. The complete ID stays on the sale record and the printed
receipt.

---

## 7. Printing

Receipt handling lives entirely in `receipt.py`. Order of operations is always:
**save the sale → archive the receipt → try to print.** A printer problem can
never undo or duplicate a sale.

Modes (configured on the POS printer screen):

| Mode | Behaviour |
|---|---|
| **Archive only** (default) | Receipts are saved silently. No printing. Best when no printer is connected. |
| **Default** | Uses the Windows default printer. Virtual devices such as "Microsoft Print to PDF" and Fax are skipped. |
| **ESC/POS** | Direct RAW printing to a 58 mm thermal printer, 32 columns, ASCII only, with a partial cut. |

No printer name is hardcoded — the app lists what Windows reports.

---

## 8. Backups and safety

- A daily backup task is registered in Windows Task Scheduler
  (**"NeighborhoodMart Daily Backup"**, 11:59 PM) and writes to
  `C:\NeighborhoodMart\backups\`.
- You can create a backup at any time in **Inventory → Database Tools**.
- Backups use SQLite's online backup API, so they are safe even while the
  database is in WAL mode. Each one is verified before it is finalized.
- Keep copies of backups somewhere other than the same PC if the data matters.

### Deleting a product
There is no hard delete in the app. **Deactivate** a product instead: the row
stays in the database so past receipts and stock history remain valid, but the
barcode stops working at the POS and the item disappears from the catalog.
If you truly need the row physically removed, see the manual SQL procedure
outside this app — but only for products with no sales or movements attached.

---

## 9. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Cashier sees only Point of Sale | Correct by design. Log in as manager for other screens. |
| Scanning does nothing | The scanner must be a keyboard-wedge (HID) type that types digits + Enter. Click the scan field first. |
| "Barcode must be 6 to 14 digits" | The code has spaces/dashes or is the wrong length. Only digits are accepted. |
| Discount not applying | Senior/PWD requires both customer name and ID number. |
| No receipt prints | Default mode is archive-only. Check the printer button on POS and the mode. |
| Product not scanning after "delete" | It was deactivated. Use Products to check, or reactivate manually. |
| App won't start | Run `run_debug.bat` to see the error, and check `logs/error.log`. After a code update, close and relaunch. |
| Database looks empty on a new install | Expected. The app starts with an empty catalog; add products with opening stock. |

---

## 10. Quick reference

- **Currency:** Philippine peso only, two decimals.
- **Selling:** scan → basket → discount (optional) → cash → confirm.
- **Stock:** Products (opening stock) on day one; Inventory (stock-in/out) after.
- **Discount:** 20% Senior/PWD, whole sale, name + ID required.
- **Reports:** manager only, daily Z-read + CSV.
- **Delete:** it's called Deactivate — history is preserved.
- **Backup:** daily automatically, or manually in Inventory → Database Tools.
