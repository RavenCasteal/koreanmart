"""
Stage 9 - receipt generation, archiving and Windows printing.

Design rules:
  * The receipt is built ONLY from a COMMITTED sale. Printing happens after
    the database transaction has returned successfully, so a printer error
    can never roll back a sale or lose a deduction.
  * Every receipt is archived as a UTF-8 text file in receipts/ before any
    print attempt. Reprint is always possible from Sales history.
  * All money is Philippine peso (PHP). formatting.format_money() is the one
    and only money formatter: "₱150.00", never a yen/yuan/dollar sign or a
    bare number. This restaurant is based in the Philippines.
  * Printing is best-effort and NEVER raises. If no printer is available the
    archived copy is still valid and the UI simply tells the cashier.

Windows printing options (both fully offline):
  1. Default printer: os.startfile(path, "print"). No extra dependency; the
     Windows file association (e.g. Notepad) handles Unicode/peso sign.
  2. Raw ESC/POS thermal printer: only when pywin32 (win32print) is installed
     AND a printer name is supplied. Sends an init command and a paper cut.
     For this path the peso sign is transliterated to "PHP " because many
     thermal code pages cannot render U+20B1.

Stage 14 - hardware printing
----------------------------
  * The raw path is now a real ESC/POS job: init, PC437 code page, per-line
    alignment, bold headers/TOTAL, line feed and a partial paper cut, sized
    for 58 mm / 32-column paper.
  * The printer is NEVER hardcoded. config/printer.json (created on demand)
    or the Printer setup dialog on the Point of Sale page selects one of the
    installed Windows printers discovered with win32print.EnumPrinters().
  * Printer presence/offline status is checked BEFORE opening a job. A failed
    ESC/POS attempt does NOT silently fall through to the default printer -
    that would produce a second, wrong receipt. The sale is already committed
    and archived, so the cashier simply uses Reprint later.
  * A short-lived in-process guard suppresses a duplicate print of the same
    archived receipt (e.g. a double-click on Confirm sale).
  * Setting MART_NO_HARDWARE=1 forces archive-only mode for automated tests.
"""

from __future__ import annotations

import json
import os
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any

import discounts
from formatting import CURRENCY_CODE, CURRENCY_SYMBOL, format_money

BASE_DIR = Path(__file__).resolve().parent
RECEIPTS_DIR = BASE_DIR / "receipts"
CONFIG_DIR = BASE_DIR / "config"
PRINTER_CONFIG_PATH = CONFIG_DIR / "printer.json"

STORE_NAME = "RAMEN HOUSE"
STORE_TAGLINE = "Regular Ramen"
MART_NAME = "NEIGHBORHOOD MART"
MART_TAGLINE = "Foods  ·  Drinks  ·  Everyday needs"
# Stage 14: the single-line tagline above is 34 characters and would wrap on
# 32-column 58 mm paper. Print it as two centred lines instead.
MART_TAGLINE_LINES = ("Foods  ·  Drinks", "Everyday needs")
RECEIPT_WIDTH = 32  # 58 mm thermal paper, monospaced

_ESC = b"\x1b"
_GS = b"\x1d"

# --- Stage 14: printer configuration (no printer name is ever hardcoded) ---
PRINTER_MODES = ("default", "escpos", "archive_only")
DEFAULT_PRINTER_CONFIG: dict[str, Any] = {
    # Stage 17: archive silently out of the box. With no receipt printer
    # installed, Windows' default is usually "Microsoft Print to PDF", and
    # os.startfile(path, "print") opens a "Save Print Output As" dialog AFTER
    # every sale. A sale must never be followed by a prompt. Printing becomes
    # automatic as soon as a real ESC/POS printer is chosen in Printer setup.
    "mode": "archive_only",   # default | escpos | archive_only
    "printer_name": "",       # exact Windows printer name from EnumPrinters
    "auto_print": True,       # print automatically after a committed sale
    "cut": True,              # partial paper cut at the end of the job
    "feed_lines": 3,          # extra blank lines before the cut
    "bold": True,             # bold store name, reprint/void marker and TOTAL
}
DUPLICATE_WINDOW_S = 1.5      # suppress an accidental second print
MAX_FEED_LINES = 20

# Stage 17: these Windows "printers" do not print on paper - they open a
# file-save dialog or discard the job. Sending a receipt to one of them is
# exactly what made the cashier answer a "Save" prompt after every sale.
VIRTUAL_PRINTER_MARKERS: tuple[str, ...] = (
    "print to pdf", "xps", "fax", "onenote", "one note", "pdfcreator",
    "bullzip", "send to", "microsoft print", "pdf writer", "print to file",
)


def is_virtual_printer(name: Any) -> bool:
    """True for file/fax pseudo-printers (and for 'no printer at all')."""
    text = str(name or "").strip().lower()
    if not text:
        return True
    return any(marker in text for marker in VIRTUAL_PRINTER_MARKERS)

# Win32 printer status bits (win32printcon names avoided: no hard dependency
# on the constants module, so a missing pywin32 is still handled cleanly).
PRINTER_STATUS_OFFLINE = 0x00000080
PRINTER_ATTRIBUTE_WORK_OFFLINE = 0x00000400


class PrinterUnavailable(Exception):
    """Raised for a missing driver, unknown printer name or offline device."""


# ----------------------------------------------------------------------
# Text layout
# ----------------------------------------------------------------------
def _separator() -> str:
    return "-" * RECEIPT_WIDTH


def _row(left: Any, right: Any) -> str:
    """Right-align a value on a fixed-width thermal line."""
    left_text = str(left)
    right_text = str(right)
    gap = RECEIPT_WIDTH - len(left_text) - len(right_text)
    if gap < 1:
        return left_text + "\n" + right_text.rjust(RECEIPT_WIDTH)
    return left_text + " " * gap + right_text


def _centre(text: str) -> str:
    return text.center(RECEIPT_WIDTH)


def _wrap(text: str, width: int = RECEIPT_WIDTH) -> list[str]:
    """Word-wrap a long void reason without breaking the receipt layout."""
    words = str(text or "").split()
    if not words:
        return []
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > width and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def build_receipt(sale: dict[str, Any], *, reprint: bool = False) -> str:
    """Return the complete fixed-width receipt text for one sale.

    Accepts either the dict returned by sales_repository.confirm_sale() /
    void_sale() or the immutable record from get_sale_detail().
    """
    status = str(sale.get("status", "completed"))
    voided = status == "voided"
    item = str(sale.get("menu_item_name") or STORE_TAGLINE)
    quantity = int(sale.get("quantity") or 0)
    unit_price = float(sale.get("unit_price") or 0.0)
    total = float(sale.get("total_amount") or 0.0)
    tendered = float(sale.get("amount_tendered") or 0.0)
    change = float(sale.get("change_amount") or 0.0)
    currency = str(sale.get("currency_code") or CURRENCY_CODE)

    lines: list[str] = [
        _centre(STORE_NAME),
        _centre(item),
        _separator(),
    ]

    if reprint:
        lines.append(_centre("--- REPRINT COPY ---"))
    if voided:
        lines.append(_centre("*** VOIDED ***"))

    lines.extend(
        [
            _row("Txn no.", str(sale.get("transaction_number") or "—")),
            _row("Date/time", str(sale.get("transaction_date") or "—")),
            _row("Cashier", str(sale.get("cashier") or "—")),
            _separator(),
            item,
            _row(
                f"{quantity} x {format_money(unit_price)}",
                format_money(total),
            ),
            _separator(),
            _row("TOTAL", format_money(total)),
            _separator(),
            _row("Payment", str(sale.get("payment_method") or "—")),
            _row("Tendered", format_money(tendered)),
            _row("Change", format_money(change)),
            _separator(),
        ]
    )

    if voided:
        lines.append(_centre("SALE CANCELLED"))
        lines.append(_row("Voided at", str(sale.get("voided_at") or "—")))
        lines.append("Reason:")
        lines.extend(_wrap(str(sale.get("void_reason") or "—")))
        lines.append(_separator())

    lines.extend(
        [
            _centre(f"Amounts in {currency}"),
            _centre("Thank you!"),
            "",
        ]
    )
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Archiving (always happens, even when printing fails)
# ----------------------------------------------------------------------
def receipt_path(
    transaction_number: str, *, reprint: bool = False, voided: bool = False
) -> Path:
    """A stable, unique filename for each kind of receipt copy."""
    safe = "".join(
        char if char.isalnum() or char in "-_" else "_"
        for char in str(transaction_number)
    )
    suffix = "-VOID" if voided else "-REPRINT" if reprint else ""
    return RECEIPTS_DIR / f"{safe}{suffix}.txt"


def archive_receipt(
    text: str,
    transaction_number: str,
    *,
    reprint: bool = False,
    voided: bool = False,
    base_dir: Any = None,
) -> Path:
    """Write the receipt to receipts/ and return the path. Always UTF-8."""
    target = receipt_path(
        transaction_number, reprint=reprint, voided=voided
    )
    if base_dir is not None:
        target = Path(base_dir) / target.name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


# ----------------------------------------------------------------------
# Stage 14 - printer configuration and discovery (best effort, no raises)
# ----------------------------------------------------------------------
def load_printer_config(path: Any = None) -> dict[str, Any]:
    """Read config/printer.json, falling back to safe defaults.

    A missing, unreadable or malformed file is never an error: the app stays
    on the silent archive-only default (Stage 17), so a sale never ends with
    a "Save Print Output As" dialog. MART_NO_HARDWARE=1 also forces it.
    """
    target = Path(path) if path is not None else PRINTER_CONFIG_PATH
    config = dict(DEFAULT_PRINTER_CONFIG)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for key in config:
                if key in raw:
                    config[key] = raw[key]
    except (OSError, ValueError, TypeError):
        pass

    config["mode"] = (
        str(config["mode"])
        if str(config["mode"]) in PRINTER_MODES
        else DEFAULT_PRINTER_CONFIG["mode"]
    )
    config["printer_name"] = str(config["printer_name"] or "").strip()
    for flag in ("auto_print", "cut", "bold"):
        config[flag] = bool(config[flag])
    try:
        config["feed_lines"] = max(0, min(int(config["feed_lines"]), MAX_FEED_LINES))
    except (TypeError, ValueError):
        config["feed_lines"] = DEFAULT_PRINTER_CONFIG["feed_lines"]
    if os.environ.get("MART_NO_HARDWARE") == "1":
        config["mode"] = "archive_only"
    return config


def save_printer_config(config: dict[str, Any], path: Any = None) -> Path:
    """Persist printer settings. Only known keys are written."""
    clean = load_printer_config(path)
    for key in clean:
        if key in config:
            clean[key] = config[key]
    # Re-validate through the loader, but never let the env override the file.
    clean["mode"] = (
        str(clean["mode"])
        if str(clean["mode"]) in PRINTER_MODES
        else DEFAULT_PRINTER_CONFIG["mode"]
    )
    clean["printer_name"] = str(clean["printer_name"] or "").strip()
    try:
        clean["feed_lines"] = max(0, min(int(clean["feed_lines"]), MAX_FEED_LINES))
    except (TypeError, ValueError):
        clean["feed_lines"] = DEFAULT_PRINTER_CONFIG["feed_lines"]
    target = Path(path) if path is not None else PRINTER_CONFIG_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(clean, indent=2, ensure_ascii=True) + "\n", encoding="utf-8"
    )
    return target


def pywin32_available() -> bool:
    """True only when win32print can actually be imported."""
    try:
        import win32print  # noqa: F401
    except Exception:
        return False
    return True


def list_printers() -> list[str]:
    """Names of installed local/network Windows printers; [] when unavailable."""
    if not sys.platform.startswith("win"):
        return []
    try:
        import win32print
    except Exception:
        return []
    try:
        flags = (
            win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        )
        entries = win32print.EnumPrinters(flags)
    except Exception:
        return []
    names: list[str] = []
    for entry in entries:
        name = None
        if isinstance(entry, (tuple, list)) and len(entry) > 2:
            name = entry[2]
        elif isinstance(entry, dict):
            name = entry.get("pPrinterName")
        text = str(name or "").strip()
        if text and text not in names:
            names.append(text)
    return sorted(names, key=str.lower)


def default_printer_name() -> str:
    """Windows default printer, or "" when there is none / pywin32 is absent."""
    if not sys.platform.startswith("win"):
        return ""
    try:
        import win32print

        return str(win32print.GetDefaultPrinter() or "")
    except Exception:
        return ""


def check_printer(printer_name: Any) -> dict[str, Any]:
    """Verify a printer is installed, reachable and online BEFORE a job.

    Returns a dict: name, available, offline, detail. Never raises - the
    caller turns a False result into a friendly cashier-facing message.
    """
    name = str(printer_name or "").strip()
    status = {
        "name": name, "available": False, "offline": False, "detail": "",
    }
    if not name:
        status["detail"] = "No receipt printer selected."
        return status
    if not sys.platform.startswith("win"):
        status["detail"] = "Direct ESC/POS printing requires Windows."
        return status
    if not pywin32_available():
        status["detail"] = (
            "pywin32 is missing. Run: .venv\\Scripts\\python.exe -m pip "
            "install pywin32"
        )
        return status
    if name not in list_printers():
        status["detail"] = (
            f'Printer "{name}" is not installed or has been disconnected. '
            "Choose another printer in Printer setup."
        )
        return status

    import win32print

    try:
        handle = win32print.OpenPrinter(name)
        try:
            info = win32print.GetPrinter(handle, 2)
            state = int(info.get("Status") or 0)
            attributes = int(info.get("Attributes") or 0)
            offline = bool(
                state & PRINTER_STATUS_OFFLINE
                or attributes & PRINTER_ATTRIBUTE_WORK_OFFLINE
            )
            status["offline"] = offline
            if offline:
                status["detail"] = (
                    f'Printer "{name}" is offline. Switch it on and reconnect '
                    "the USB cable."
                )
                return status
        finally:
            win32print.ClosePrinter(handle)
    except Exception as exc:
        status["detail"] = f'Cannot open printer "{name}": {exc}'
        return status
    status["available"] = True
    status["detail"] = "Printer is ready."
    return status


def printer_settings(path: Any = None) -> dict[str, Any]:
    """Everything the Printer setup dialog needs in one call."""
    return {
        "config": load_printer_config(path),
        "printers": list_printers(),
        "default": default_printer_name(),
        "pywin32": pywin32_available(),
        "windows": sys.platform.startswith("win"),
    }


# ----------------------------------------------------------------------
# Stage 14 - ESC/POS byte stream for 58 mm / 32-column thermal paper
# ----------------------------------------------------------------------
def _to_ascii(text: Any) -> str:
    """Transliterate to single-width ASCII so columns stay at 32 chars.

    Thermal printers use PC437 and cannot render U+20B1 (peso sign), en/em
    dashes or middle dots. "₱150.00" becomes "P150.00" (same width), while
    the ARCHIVED receipt keeps the real peso sign.
    """
    replaced = (
        str(text)
        .replace(CURRENCY_SYMBOL, "P")
        .replace("\u2014", "-")
        .replace("\u2013", "-")
        .replace("\u2212", "-")
        .replace("\u00b7", "*")
        .replace("\u2019", "'")
    )
    return (
        unicodedata.normalize("NFKD", replaced)
        .encode("ascii", "replace")
        .decode("ascii")
    )


def build_escpos_bytes(
    text: str, *,
    cut: bool = True,
    feed_lines: int = 3,
    bold: bool = True,
) -> bytes:
    """Build a raw ESC/POS job from the already archived 58 mm receipt text.

    Commands used (all standard ESC/POS):
      ESC @          initialize
      ESC t 0        select PC437 code page
      ESC a n        alignment (0 left, 1 centre)
      ESC E n        emphasised / bold on (1) and off (0)
      GS ! 0         reset character size
      ESC d n        feed n lines
      GS V B 0       partial paper cut
    Lines are CRLF terminated, as required by virtually all thermal printers.
    """
    feed = max(0, min(int(feed_lines), MAX_FEED_LINES))
    out = bytearray()
    out += _ESC + b"@"          # initialize
    out += _ESC + b"t\x00"      # PC437
    out += _GS + b"!\x00"       # normal character size
    out += _ESC + b"a\x00"      # left align
    out += _ESC + b"E\x00"      # bold off

    seen_title = False
    for line in str(text).splitlines():
        stripped = line.strip()
        if not stripped:
            out += b"\r\n"
            continue
        # _centre() leaves BOTH leading and trailing padding; use the
        # printer's own centre command instead of counting spaces.
        centred = line[:1].isspace() and line[-1:].isspace()
        is_title = not seen_title
        seen_title = True
        content = _to_ascii(stripped if centred else line)
        emphasise = bold and (
            is_title or stripped.startswith("TOTAL")
            or stripped.startswith("***") or stripped.startswith("---")
        )
        out += _ESC + (b"a\x01" if centred else b"a\x00")
        if emphasise:
            out += _ESC + b"E\x01"
        out += content.encode("ascii", "replace") + b"\r\n"
        if emphasise:
            out += _ESC + b"E\x00"

    out += _ESC + b"a\x00" + _ESC + b"E\x00"   # leave printer in a clean state
    out += _ESC + b"d" + bytes([feed])
    if cut:
        out += _GS + b"VB\x00"                  # partial cut
    return bytes(out)


def _to_printer_bytes(text: str) -> bytes:
    """Backwards-compatible alias for the full ESC/POS payload."""
    return build_escpos_bytes(text)


def _raw_print(
    text: str, printer_name: str, config: dict[str, Any] | None = None
) -> str:
    """Send one RAW ESC/POS job. Raises PrinterUnavailable on any failure."""
    settings = config or load_printer_config()
    status = check_printer(printer_name)
    if not status["available"]:
        raise PrinterUnavailable(status["detail"])

    import win32print  # optional dependency; imported only when needed

    payload = build_escpos_bytes(
        text,
        cut=bool(settings["cut"]),
        feed_lines=int(settings["feed_lines"]),
        bold=bool(settings["bold"]),
    )
    handle = win32print.OpenPrinter(printer_name)
    doc_open = False
    try:
        job = win32print.StartDocPrinter(
            handle, 1, ("Neighborhood Mart Receipt", None, "RAW")
        )
        doc_open = True
        win32print.StartPagePrinter(handle)
        win32print.WritePrinter(handle, payload)
        win32print.EndPagePrinter(handle)
        win32print.EndDocPrinter(handle)
        doc_open = False
        return (
            f"ESC/POS job {job}: {len(payload)} bytes sent to "
            f"{printer_name}."
        )
    finally:
        if doc_open:
            try:
                win32print.AbortPrinter(handle)
            except Exception:
                pass
        win32print.ClosePrinter(handle)


def print_test_page(printer_name: Any = None) -> dict[str, Any]:
    """Hardware check that does not create or archive a sale."""
    settings = load_printer_config()
    name = str(printer_name or "").strip() or settings["printer_name"]
    status = check_printer(name)
    if not status["available"]:
        return {"ok": False, "method": "escpos_failed", "detail": status["detail"]}
    text = "\n".join(
        [
            _centre(MART_NAME),
            _centre("PRINTER TEST"),
            _separator(),
            _row("Printer", name[:18]),
            _row("Paper", "58 mm / 32 col"),
            _row("Currency", CURRENCY_CODE),
            _row("Test amount", format_money(123.45)),
            _separator(),
            _centre("If you can read this,"),
            _centre("the printer is ready."),
            "",
        ]
    )
    try:
        detail = _raw_print(text, name, settings)
    except Exception as exc:
        return {
            "ok": False, "method": "escpos_failed",
            "detail": f"Test print failed: {exc}",
        }
    return {"ok": True, "method": "escpos", "detail": detail}


class _PrintGuard:
    """Suppress a second print of the same archived file in a short window."""

    def __init__(self, window: float = DUPLICATE_WINDOW_S) -> None:
        self.window = float(window)
        self.recent: dict[str, float] = {}

    def is_duplicate(self, key: str, now: float | None = None) -> bool:
        stamp = self.recent.get(key)
        if stamp is None:
            return False
        return (time.monotonic() if now is None else now) - stamp < self.window

    def record(self, key: str, now: float | None = None) -> None:
        # Keep the dictionary tiny: no receipt history is retained in memory.
        if len(self.recent) > 64:
            self.recent.clear()
        self.recent[key] = time.monotonic() if now is None else now

    def clear(self) -> None:
        self.recent.clear()


_PRINT_GUARD = _PrintGuard()


def reset_print_guard() -> None:
    """Test helper: forget all recent print jobs."""
    _PRINT_GUARD.clear()


# ----------------------------------------------------------------------
# Printing (best effort - never raises)
# ----------------------------------------------------------------------
def print_receipt(
    text: str,
    path: Any = None,
    printer_name: str | None = None,
) -> dict[str, Any]:
    """Try to print. Returns a status dict; never raises.

    The caller has already committed the sale, so any failure here is only a
    printing problem - the cashier can use Reprint from Sales history.
    An explicit printer_name always means RAW ESC/POS; otherwise the mode in
    config/printer.json decides between ESC/POS, the Windows default printer
    and archive-only. A failed ESC/POS attempt NEVER falls through to a
    second printer, so a hardware fault cannot produce a duplicate receipt.

    Stage 17: a "Windows default printer" that is really a PDF/fax writer is
    skipped silently. Those devices always open a Save dialog; a completed
    sale must never be held up by one. The receipt is already archived.
    """
    result: dict[str, Any] = {"method": "archive_only", "detail": "", "ok": True}
    config = load_printer_config()
    if not config["auto_print"]:
        result["method"] = "disabled"
        result["detail"] = "Automatic printing is off; receipt archived."
        return result

    mode = config["mode"]
    selected = str(printer_name or "").strip() or config["printer_name"]
    explicit = bool(str(printer_name or "").strip())
    if explicit:
        mode = "escpos"

    key = str(path) if path is not None else None
    if key and _PRINT_GUARD.is_duplicate(key):
        result["method"] = "duplicate_skipped"
        result["detail"] = (
            "This receipt was just printed - duplicate suppressed. "
            "Use Reprint if you need another copy."
        )
        return result

    if mode == "archive_only":
        result["detail"] = (
            "Receipt archived automatically. Set up a receipt printer for "
            "automatic printing."
        )
        return result

    if mode == "escpos":
        if not selected:
            result["method"] = "escpos_failed"
            result["ok"] = False
            result["detail"] = (
                "No receipt printer selected. Open Printer setup on the "
                "Point of Sale page. Receipt was archived."
            )
            return result
        try:
            detail = _raw_print(text, selected, config)
        except Exception as exc:  # driver missing / unplugged / offline
            result["method"] = "escpos_failed"
            result["ok"] = False
            result["detail"] = (
                f"ESC/POS print failed: {exc} Receipt was archived - "
                "use Reprint when the printer is back."
            )
            return result
        result["method"] = "escpos"
        result["detail"] = detail
        if key:
            _PRINT_GUARD.record(key)
        return result

    # Original Stage 9 behaviour: Windows default printer via file association.
    if not sys.platform.startswith("win") or path is None:
        result["detail"] = "No Windows printer configured; receipt archived."
        return result
    # Stage 17: "Microsoft Print to PDF", Fax, XPS, OneNote etc. are NOT
    # paper printers. Printing to them makes Windows ask the cashier where to
    # save a file after EVERY sale. Archive silently instead - no dialog.
    name = default_printer_name()
    if is_virtual_printer(name):
        result["detail"] = (
            f"No paper printer is set as the Windows default "
            f"({name or 'none found'}). Receipt archived automatically - "
            "choose a receipt printer in Printer setup to print."
        )
        return result
    try:
        os.startfile(str(path), "print")  # type: ignore[attr-defined]
    except Exception as exc:
        result["method"] = "failed"
        result["ok"] = False
        result["detail"] = f"Could not print: {exc}. Receipt was archived."
        return result
    result["method"] = "default_printer"
    result["detail"] = "Sent to the Windows default printer."
    if key:
        _PRINT_GUARD.record(key)
    return result


def issue_receipt(
    sale: dict[str, Any],
    *,
    reprint: bool = False,
    printer_name: str | None = None,
    base_dir: Any = None,
    auto_print: bool = True,
) -> dict[str, Any]:
    """Build + archive + attempt to print. One call for every completed sale.

    auto_print=False is used by automated tests and by a cashier who only
    wants a preview. The receipt is STILL archived in that case.
    """
    text = build_receipt(sale, reprint=reprint)
    number = str(sale.get("transaction_number") or "receipt")
    path = archive_receipt(
        text,
        number,
        reprint=reprint,
        voided=str(sale.get("status", "")) == "voided",
        base_dir=base_dir,
    )
    if auto_print:
        printed = print_receipt(text, path=path, printer_name=printer_name)
    else:
        printed = {
            "method": "skipped",
            "detail": "Printing skipped; receipt archived.",
            "ok": True,
        }
    return {"text": text, "path": path, "print": printed}


# ----------------------------------------------------------------------
# Stage 12 - multi-line convenience-store receipts
# ----------------------------------------------------------------------
def build_store_receipt(sale: dict[str, Any], *, reprint: bool = False) -> str:
    """Receipt for a CART sale: one line per scanned product.

    Accepts the dict returned by store_sales.confirm_sale() or the immutable
    record returned by store_sales.get_sale_detail(). Every value on the
    receipt comes from the sale snapshot, never from the live catalog, so a
    later rename or price change cannot rewrite history.
    """
    status = str(sale.get("status", "completed"))
    voided = status == "voided"
    # Stage 15: legacy sales have no discount columns, so normalise first.
    sale = discounts.apply_to_sale(sale)
    items = list(sale.get("items") or [])
    subtotal = float(sale["subtotal_amount"])
    total = float(sale.get("total_amount") or 0.0)
    discount_amount = float(sale["discount_amount"])
    discount_label = str(sale.get("discount_label") or "")
    tendered = float(sale.get("amount_tendered") or 0.0)
    change = float(sale.get("change_amount") or 0.0)
    currency = str(sale.get("currency_code") or CURRENCY_CODE)

    lines: list[str] = [_centre(MART_NAME)]
    lines.extend(_centre(line) for line in MART_TAGLINE_LINES)
    lines.append(_separator())
    if reprint:
        lines.append(_centre("--- REPRINT COPY ---"))
    if voided:
        lines.append(_centre("*** VOIDED ***"))

    lines.extend(
        [
            _row("Txn no.", str(sale.get("transaction_number") or "—")),
            _row("Date/time", str(sale.get("transaction_date") or "—")),
            _row("Cashier", str(sale.get("cashier") or "—")),
            _separator(),
        ]
    )

    for item in items:
        name = str(item.get("product_name") or "Product")
        code = str(item.get("barcode") or "")
        quantity = int(float(item.get("quantity") or 0))
        price = float(item.get("unit_price") or 0.0)
        line_total = float(item.get("line_total") or 0.0)
        # Long product names must wrap, not overflow the 58 mm paper.
        lines.extend(_wrap(name) or [name[:RECEIPT_WIDTH]])
        if code:
            lines.append(f"[{code}]"[:RECEIPT_WIDTH])
        lines.append(
            _row(
                f"{quantity} x {format_money(price)}",
                format_money(line_total),
            )
        )
    if not items:
        lines.append(str(sale.get("store") or "No items"))

    lines.extend([_separator(), _row("Subtotal", format_money(subtotal))])
    if discount_amount > 0:
        short = discounts.short_label(sale["discount_type"]) or "Discount"
        lines.append(_row(short, "-" + format_money(discount_amount)))
        lines.append(_row("Discount type", discount_label[:14]))
    lines.extend(
        [
            # Stage 15: TOTAL is the NET amount actually payable, after the
            # Senior/PWD discount. Both lines are always shown for auditing.
            _row("TOTAL", format_money(total)),
            _separator(),
            _row("Payment", str(sale.get("payment_method") or "—")),
            _row("Tendered", format_money(tendered)),
            _row("Change", format_money(change)),
            _separator(),
        ]
    )
    if discount_amount > 0:
        # Proof of eligibility is printed on the receipt, as required for
        # senior citizen / PWD discount auditing.
        lines.append(_centre("DISCOUNT DETAILS"))
        lines.extend(_wrap(str(sale["customer_name"] or "—")))
        lines.append(_row("ID no.", str(sale["customer_id"] or "—")[:16]))
        lines.append(_separator())

    if voided:
        lines.append(_centre("SALE CANCELLED"))
        lines.append(_row("Voided at", str(sale.get("voided_at") or "—")))
        lines.append("Reason:")
        lines.extend(_wrap(str(sale.get("void_reason") or "—")))
        lines.append(_separator())

    lines.extend(
        [
            _centre(f"Amounts in {currency}"),
            _centre("Salamat, thank you!"),
            "",
        ]
    )
    return "\n".join(lines)


def issue_store_receipt(
    sale: dict[str, Any],
    *,
    reprint: bool = False,
    printer_name: str | None = None,
    base_dir: Any = None,
    auto_print: bool = True,
) -> dict[str, Any]:
    """Build + archive + best-effort print for a store sale.

    Called ONLY after confirm_sale()/void_sale() has committed. A printer
    failure never rolls back a sale; the archived copy can be reprinted from
    Sales history. auto_print=False is used by tests and GUI smoke runs.
    """
    text = build_store_receipt(sale, reprint=reprint)
    number = str(sale.get("transaction_number") or "receipt")
    path = archive_receipt(
        text,
        number,
        reprint=reprint,
        voided=str(sale.get("status", "")) == "voided",
        base_dir=base_dir,
    )
    if auto_print:
        printed = print_receipt(text, path=path, printer_name=printer_name)
    else:
        printed = {
            "method": "skipped",
            "detail": "Printing skipped; receipt archived.",
            "ok": True,
        }
    return {"text": text, "path": path, "print": printed}
