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
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from formatting import CURRENCY_CODE, CURRENCY_SYMBOL, format_money

BASE_DIR = Path(__file__).resolve().parent
RECEIPTS_DIR = BASE_DIR / "receipts"

STORE_NAME = "RAMEN HOUSE"
STORE_TAGLINE = "Regular Ramen"
MART_NAME = "NEIGHBORHOOD MART"
MART_TAGLINE = "Foods  ·  Drinks  ·  Everyday needs"
RECEIPT_WIDTH = 32  # 58 mm thermal paper, monospaced

_ESC = b"\x1b"
_GS = b"\x1d"


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
# Printing (best effort - never raises)
# ----------------------------------------------------------------------
def _to_printer_bytes(text: str) -> bytes:
    """ESC/POS payload. Transliterate ₱ because thermal code pages vary."""
    safe = text.replace(CURRENCY_SYMBOL, "PHP ")
    return _ESC + b"@" + safe.encode("utf-8", "replace") + b"\n\n\n" + _GS + b"VB\x00"


def _raw_print(text: str, printer_name: str) -> str:
    import win32print  # optional dependency; imported only when needed

    handle = win32print.OpenPrinter(printer_name)
    try:
        job = win32print.StartDocPrinter(handle, 1, ("Ramen POS Receipt", None, "RAW"))
        win32print.StartPagePrinter(handle)
        win32print.WritePrinter(handle, _to_printer_bytes(text))
        win32print.EndPagePrinter(handle)
        win32print.EndDocPrinter(handle)
        return f"Sent to {printer_name} (job {job})"
    finally:
        win32print.ClosePrinter(handle)


def print_receipt(
    text: str,
    path: Any = None,
    printer_name: str | None = None,
) -> dict[str, Any]:
    """Try to print. Returns a status dict; never raises.

    The caller has already committed the sale, so any failure here is only a
    printing problem - the cashier can use Reprint from Sales history.
    """
    result: dict[str, Any] = {"method": "archive_only", "detail": "", "ok": True}

    if printer_name:
        try:
            result["detail"] = _raw_print(text, printer_name)
            result["method"] = "escpos"
            return result
        except Exception as exc:  # pywin32 missing / printer offline
            result["detail"] = f"ESC/POS print failed: {exc}"

    if not sys.platform.startswith("win") or path is None:
        result["detail"] = (
            result["detail"] or "No Windows printer configured; receipt archived."
        )
        return result

    try:
        os.startfile(str(path), "print")  # type: ignore[attr-defined]
        result["method"] = "default_printer"
        result["detail"] = "Sent to the Windows default printer."
        return result
    except Exception as exc:
        result["method"] = "failed"
        result["ok"] = False
        result["detail"] = f"Could not print: {exc}. Receipt was archived."
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
    items = list(sale.get("items") or [])
    total = float(sale.get("total_amount") or 0.0)
    tendered = float(sale.get("amount_tendered") or 0.0)
    change = float(sale.get("change_amount") or 0.0)
    currency = str(sale.get("currency_code") or CURRENCY_CODE)

    lines: list[str] = [
        _centre(MART_NAME),
        _centre(MART_TAGLINE),
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
        ]
    )

    for item in items:
        name = str(item.get("product_name") or "Product")
        code = str(item.get("barcode") or "")
        quantity = int(float(item.get("quantity") or 0))
        price = float(item.get("unit_price") or 0.0)
        line_total = float(item.get("line_total") or 0.0)
        lines.append(name)
        if code:
            lines.append(f"[{code}]")
        lines.append(
            _row(
                f"{quantity} x {format_money(price)}",
                format_money(line_total),
            )
        )
    if not items:
        lines.append(str(sale.get("store") or "No items"))

    lines.extend(
        [
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
