# COOLITS Korean Mart - Inventory & POS System

A Windows-local, offline desktop application for COOLITS Korean Mart
(Pasalubong Center) with barcode scanning, inventory management, and
receipt printing.

**For store owners and daily use, see [USER_GUIDE.md](USER_GUIDE.md).**
It covers setup, selling, stock, reports, printing, backups and troubleshooting.

## Tech Stack

- **Python 3.13** (standard library only, plus CustomTkinter + Pillow)
- **SQLite** for local data storage (no server, no cloud)
- **CustomTkinter 5.2.2** for the UI
- **Pillow 11.3.0** for icons/images
- **PBKDF2-HMAC-SHA256** for password hashing (200k iterations)
- **pywin32** (optional, Windows-only) for ESC/POS thermal printer support

## Prerequisites

| Requirement | Needed? | Where to get it |
|---|---|---|
| **Python 3.13 (Windows 10/11)** | Yes | [python.org/downloads/windows](https://www.python.org/downloads/windows/). Download the 64-bit installer. During setup, check **"Add python.exe to PATH"** and leave **tcl/tk** enabled (it is on by default — CustomTkinter needs it). |
| **CustomTkinter 5.2.x** | Yes | Installed automatically from `requirements.txt` (see below). |
| **Pillow 11.x** | Yes | Installed automatically from `requirements.txt`. |
| **pywin32** | Optional | Included in `requirements.txt` on Windows. Only used for direct ESC/POS printing to a thermal receipt printer; the app runs without it. |
| **SQLite** | No | Built into Python as the `sqlite3` module. Nothing to install. |
| **DB Browser for SQLite** | No | Optional viewer only, for inspecting the `.db` file by hand. The app does not use it. |

You need the **entire project folder**, not just `main.py` — the launchers rely on
`pages/`, `components/`, `assets/` and the other files sitting beside it.

## Quick Start

1. **Get the code.** On the GitHub page click **Code → Download ZIP**, then
   unzip it to a folder such as `C:\NeighborhoodMart`. Alternatively, if you
   have Git installed:
   ```
   git clone https://github.com/RavenCasteal/koreanmart.git NeighborhoodMart
   ```

2. **Open a terminal in that folder** (in File Explorer, click the address bar,
   type `cmd`, and press Enter).

3. **Create a virtual environment and install dependencies:**
   ```
   python -m venv .venv
   .venv\Scripts\python.exe -m pip install -r requirements.txt
   ```
   This installs CustomTkinter, Pillow, and (on Windows) pywin32. Their smaller
   dependencies (`darkdetect`, `packaging`) are pulled in automatically.

4. **Run the app.** Double-click `run.bat`, or from the terminal:
   ```
   .venv\Scripts\python.exe main.py
   ```
   Use `run_debug.bat` instead if you need to see error output in a console.

5. **First login:**
   - Username: `admin`
   - Password: see `database.py` (`DEFAULT_ADMIN_PASSWORD`)
   - You will be forced to change the password on first login. Change it before
     using the app on a real counter — the default is public in this repository.

The database, `receipts/`, `logs/` and `backups/` folders are all created
automatically on first launch. No manual database setup is required.

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

Non-commercial educational use only. See `LICENSE` for full terms.
