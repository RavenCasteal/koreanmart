"""
Stage 15 - four-function cashier calculator (pure logic, no Tk).

The Point of Sale needs quick arithmetic for change, split payments and
"how much is 3 x 35". This is a small state machine, NOT eval() and NOT a
parser: a cashier can only press digits, one decimal point and the four
operators, so malformed input and code injection are impossible.

    calc = Calculator()
    calc.press("3"); calc.press("x"); calc.press("5"); calc.press("=")
    calc.value  ->  15.0

Divide-by-zero puts the calculator into a recoverable "Error" state; the next
digit starts a fresh calculation. The GUI only reads display / value.
"""

from __future__ import annotations

import math
from typing import Any

MAX_DIGITS = 12
ERROR_TEXT = "Error"

_KEY_ALIASES: dict[str, str] = {
    "*": "x", "X": "x", "\u00d7": "x", "\u00f7": "/",
    "C": "clear", "c": "clear", "CE": "clear", "Clear": "clear",
    "\u232b": "back", "back": "back", "BS": "back", "Back": "back",
    "Del": "back", "del": "back",
    "=": "equals", "Enter": "equals", "equals": "equals",
    "+/-": "negate", "%": "percent",
}
_OPERATORS = ("+", "-", "x", "/")


def _format(number: float) -> str:
    """Compact display text without binary floating-point noise."""
    number = round(float(number), 10)
    if number == 0:
        number = 0.0
    text = f"{number:.10f}".rstrip("0").rstrip(".")
    if text in ("", "-0", "-"):
        return "0"
    if len(text) > MAX_DIGITS + 8:
        return f"{number:.2f}"
    return text


class Calculator:
    """A simple pending-operation calculator."""

    def __init__(self) -> None:
        self.display = "0"
        self._pending: float | None = None
        self._operator: str | None = None
        self._fresh = True
        self._error = False
        self.clear()

    def clear(self) -> str:
        self.display = "0"
        self._pending = None
        self._operator = None
        self._fresh = True
        self._error = False
        return self.display

    @property
    def error(self) -> bool:
        return self._error

    @property
    def value(self) -> float:
        """Current numeric value; 0.0 while in the Error state."""
        if self._error:
            return 0.0
        try:
            number = float(self.display)
        except (TypeError, ValueError):
            return 0.0
        return number if math.isfinite(number) else 0.0

    @staticmethod
    def _apply(operator: str, left: float, right: float) -> float:
        if operator == "+":
            return left + right
        if operator == "-":
            return left - right
        if operator == "x":
            return left * right
        if operator == "/":
            if abs(right) < 1e-12:
                raise ZeroDivisionError("Cannot divide by zero.")
            return left / right
        raise ValueError("Unknown operator.")

    def _fail(self) -> None:
        self.display = ERROR_TEXT
        self._pending = None
        self._operator = None
        self._fresh = True
        self._error = True

    def _set_number(self, number: float) -> None:
        if not math.isfinite(number):
            self._fail()
            return
        self.display = _format(number)
        self._fresh = True

    def backspace(self) -> str:
        if self._error:
            return self.clear()
        if self._fresh or len(self.display) <= 1:
            self.display = "0"
            self._fresh = True
            return self.display
        self.display = self.display[:-1]
        if self.display in ("", "-"):
            self.display = "0"
            self._fresh = True
        return self.display

    def press(self, key: Any) -> str:
        """Apply one keypad key and return the new display text."""
        raw = str(key if key is not None else "").strip()
        # The POS keypad has a "00" key. Handle it here so the pure logic and
        # the GUI agree on exactly the same set of keys (one source of truth).
        if raw == "00":
            self.press("0")
            return self.press("0")
        action = _KEY_ALIASES.get(raw, raw)

        if self._error and action not in ("clear", "back"):
            self.clear()
        if action == "clear":
            return self.clear()
        if action == "back":
            return self.backspace()

        if action == "negate":
            if not self._fresh and self.display.startswith("-"):
                self.display = self.display[1:]
            elif self.display != "0":
                self.display = "-" + self.display
            return self.display

        if action == "percent":
            self._set_number(self.value / 100.0)
            return self.display

        if action in _OPERATORS:
            if self._operator is not None and not self._fresh:
                try:
                    result = self._apply(self._operator, self._pending or 0.0, self.value)
                except ZeroDivisionError:
                    self._fail()
                    return self.display
                self._set_number(result)
                self._pending = self.value
            else:
                self._pending = self.value
            self._operator = action
            self._fresh = True
            return self.display

        if action == "equals":
            if self._operator is not None and self._pending is not None:
                try:
                    result = self._apply(self._operator, self._pending, self.value)
                except ZeroDivisionError:
                    self._fail()
                    return self.display
                self._set_number(result)
            self._pending = None
            self._operator = None
            self._fresh = True
            return self.display

        if action in (".", ","):
            if self._fresh:
                self.display = "0."
                self._fresh = False
            elif "." not in self.display:
                self.display += "."
            return self.display

        if len(action) == 1 and action.isdigit():
            if self._fresh:
                self.display = action
                self._fresh = False
            elif self.display == "0":
                self.display = action
            elif self.display.count(".") == 0 and len(self.display) >= MAX_DIGITS:
                return self.display
            else:
                self.display += action
            return self.display

        raise ValueError(f"Unknown calculator key: {raw!r}.")
