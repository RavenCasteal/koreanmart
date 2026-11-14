"""
Stage 14 - keyboard-wedge barcode scanner input rules (pure logic, no Tk).

A USB scanner in HID mode is just a keyboard. It types the digits of a code
with only a few milliseconds between keystrokes and then sends Enter (CR),
while a person typing the same digits is much slower. This module turns that
raw keystroke stream into ONE validated barcode:

    ScanBuffer   - decides whether a burst of keystrokes came from a scanner
    normalize_barcode() - rejects partial / accidental / non-barcode input
    ScanDebouncer - ignores a duplicate scan caused by a bouncing Enter

There is deliberately no database, GUI or third-party dependency here.
pages/store_pos.py owns the widgets and calls into this module, so the rules
can be unit-tested without a display and simulated keyboard tests can drive
the exact same code path as a real scanner.

Compatibility notes
-------------------
  * Configure the scanner for a CR / Enter suffix (see the setup guide).
  * The catalog uses EAN-13 numeric codes, so burst detection watches digits
    only. Manual entry still accepts alphanumeric codes (Code 128 etc.).
  * Some scanners add a prefix/suffix such as STX/ETX; normalize_barcode()
    drops non-printable characters before validation.
"""

from __future__ import annotations

import re
from typing import Any

# EAN-13 / UPC / Code 128 codes in this store are 8-14 characters. A wider
# range is accepted so internal or supplier barcodes still work.
MIN_BARCODE_LENGTH = 6
MAX_BARCODE_LENGTH = 32

# HID scanners type with roughly 5-20 ms between characters. A person rarely
# stays under 80 ms for an entire code.
SCAN_KEY_GAP_MS = 80.0
MIN_BURST_CHARS = 3

# A bouncing scanner trigger / double Enter must not ring the item up twice.
DEBOUNCE_MS = 150.0

_ALLOWED = re.compile(r"^[A-Za-z0-9\-_.]+$")


def normalize_barcode(value: Any) -> str:
    """Validate one COMPLETE barcode, raising ValueError with a clear message.

    Scanners may append CR/LF, a tab, or STX/ETX framing characters. Those are
    discarded. A short code (a partial scan, a stray key, a human still
    typing) is rejected so it can never be looked up and ring up the wrong
    product by accident.
    """
    raw = str(value if value is not None else "")
    clean = "".join(char for char in raw if char.isprintable()).strip()
    if not clean:
        raise ValueError("Scan or type a barcode first.")
    if len(clean) < MIN_BARCODE_LENGTH:
        raise ValueError(
            f"Incomplete barcode ({len(clean)} characters). "
            "Aim the scanner at the whole barcode and try again."
        )
    if len(clean) > MAX_BARCODE_LENGTH:
        raise ValueError(
            f"Barcode is too long ({len(clean)} characters). "
            "Clear the field and scan again."
        )
    if not _ALLOWED.fullmatch(clean):
        raise ValueError(
            "That is not a barcode. Use digits, letters, '-', '_' or '.' only."
        )
    return clean


def is_barcode_char(char: Any) -> bool:
    """True for characters that can START/continue a scanner burst."""
    return isinstance(char, str) and len(char) == 1 and char.isdigit()


class ScanBuffer:
    """Accumulate a fast keystroke burst until Enter completes the scan.

    feed(char, now_ms) returns True once the burst looks like a scanner;
    finish() returns the validated barcode, or None when the keystrokes were
    ordinary human typing (so the widget's normal Enter handling can run).
    """

    def __init__(
        self,
        gap_ms: float = SCAN_KEY_GAP_MS,
        min_burst: int = MIN_BURST_CHARS,
    ) -> None:
        self.gap_ms = float(gap_ms)
        self.min_burst = int(min_burst)
        self.chars: list[str] = []
        self.last_ms: float | None = None

    @property
    def active(self) -> bool:
        return len(self.chars) >= self.min_burst

    @property
    def value(self) -> str:
        return "".join(self.chars)

    def reset(self) -> None:
        self.chars = []
        self.last_ms = None

    def feed(self, char: Any, now_ms: float) -> bool:
        """Feed one printable keystroke; True when a scanner burst is active."""
        if not is_barcode_char(char):
            self.reset()
            return False
        moment = float(now_ms)
        if (
            self.last_ms is not None
            and 0 <= moment - self.last_ms <= self.gap_ms
        ):
            self.chars.append(char)
        else:
            self.chars = [char]
        self.last_ms = moment
        if len(self.chars) > MAX_BARCODE_LENGTH + 8:
            self.reset()
            return False
        return self.active

    def finish(self) -> str | None:
        """Validate and return the barcode if Enter ended a scanner burst."""
        if not self.active:
            self.reset()
            return None
        value = self.value
        self.reset()
        try:
            return normalize_barcode(value)
        except ValueError:
            return None


class ScanDebouncer:
    """Reject an identical scan repeated within a short window."""

    def __init__(self, window_ms: float = DEBOUNCE_MS) -> None:
        self.window_ms = float(window_ms)
        self.last_code: str | None = None
        self.last_ms: float | None = None

    def accept(self, code: str, now_ms: float) -> bool:
        moment = float(now_ms)
        if (
            self.last_code == code
            and self.last_ms is not None
            and 0 <= moment - self.last_ms <= self.window_ms
        ):
            return False
        self.last_code = code
        self.last_ms = moment
        return True

    def reset(self) -> None:
        self.last_code = None
        self.last_ms = None
