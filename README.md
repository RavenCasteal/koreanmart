# COOLITS Korean Mart - Inventory & POS System

A Windows-local, offline desktop application for COOLITS Korean Mart
(Pasalubong Center) with barcode scanning, inventory management, and
receipt printing.

## Tech Stack

- **Python 3.13** (standard library only, plus CustomTkinter + Pillow)
- **SQLite** for local data storage (no server, no cloud)
- **CustomTkinter 5.2.2** for the UI
- **Pillow 11.3.0** for icons/images
- **PBKDF2-HMAC-SHA256** for password hashing (200k iterations)
- **pywin32** (optional, Windows-only) for ESC/POS thermal printer support

## Quick Start

1. Create a virtual environment:
   ```
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. Run the app:
   ```
   python main.py
   ```
   Or use the batch files:
   ```
   run.bat         (windowed mode, pythonw)
   run_debug.bat   (console mode, shows errors)
   ```

3. First login:
   - Username: `admin`
   - Password: see `database.py` (`DEFAULT_ADMIN_PASSWORD`)
   - You will be forced to change the password on first login.

## Features

- **Barcode POS** - scan products with a USB HID keyboard-wedge scanner
- **Product catalog** - add/edit products with barcode, category, price
- **Stock management** - stock-in/out with batch/lot tracking and FEFO
- **Sales reports** - daily summary, Z-read, CSV export
- **Senior/PWD discount** - 20% off the whole sale with ID capture
- **Auto change calculation** - Quick cash buttons
- **Receipt archiving** - silent archiving (no Save prompt)
- **ESC/POS printing** - 58mm thermal printer support (optional)
- **RBAC** - manager (full access) and cashier (POS only) roles
- **Backup/Restore** - WAL-safe SQLite online backup API

## Database

The SQLite database is created automatically on first launch at
`database/ramen_inventory.db`. The schema is versioned (currently v4)
with additive migrations - no manual setup needed.

## Testing

```
.venv\Scripts\python.exe -m unittest discover -s tests
```

## Project Structure

```
main.py              - Entry point, page registration
database.py          - SQLite connection, schema, seeding, auth
products.py          - Product catalog + schema migrations
store_sales.py       - POS sale confirmation + voiding
receipt.py           - Receipt archiving + ESC/POS printing
scanner.py           - Barcode scanner logic
permissions.py       - RBAC (manager/cashier roles)
backups.py           - WAL-safe backup/restore
batches.py           - Batch/lot inventory with FEFO allocation
fresh_data_reset.py  - One-time client deployment data reset
pages/               - UI pages (dashboard, POS, inventory, etc.)
components/          - Reusable UI (sidebar, header, dialog)
tests/               - Unit tests (892 tests)
```

## License

This is a capstone project. No license granted for redistribution.
