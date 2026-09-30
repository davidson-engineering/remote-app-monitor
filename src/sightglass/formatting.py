"""Value formatting and ANSI terminal styling."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

RESET = "\x1b[0m"

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")

_COLORS = {
    "black": 0,
    "red": 1,
    "green": 2,
    "yellow": 3,
    "blue": 4,
    "magenta": 5,
    "cyan": 6,
    "white": 7,
}


def visible_len(text: str) -> int:
    """Length of ``text`` as it appears on screen, ignoring ANSI escape codes."""
    return len(_ANSI_ESCAPE.sub("", text))


def fit(text: str, width: int) -> str:
    """Pad or truncate ``text`` to exactly ``width`` visible characters."""
    length = visible_len(text)
    if length <= width:
        return text + " " * (width - length)
    plain = _ANSI_ESCAPE.sub("", text)
    return plain[: max(width - 1, 0)] + "…" if width else ""


def as_number(value: Any) -> int | float | None:
    """Return ``value`` as a number, or None if it isn't one.

    Numeric strings are parsed (ints stay ints). Booleans are not numbers here.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text)
        except ValueError:
            pass
        try:
            return float(text)
        except ValueError:
            return None
    return None


def default_text(value: Any) -> str:
    """Text for a value that has no :class:`TextFormat`.

    Numbers that need more than 6 significant digits are shortened
    (``18.627682319492283`` becomes ``"18.6277"``); anything else, including a
    device's own formatting such as ``"1.50"``, is shown exactly as given.
    """
    number = as_number(value)
    if isinstance(number, float) and math.isfinite(number):
        short = f"{number:.6g}" if abs(number) < 1e6 else f"{number:.0f}"
        if float(short) != number:
            return short
    return str(value)


@dataclass(frozen=True)
class TextFormat:
    """How to turn a value into display text.

    Numbers (including numeric strings) are rounded to ``precision`` and padded to
    ``width`` characters, where the width includes the sign and decimal point.
    With the default space padding the sign sits next to the digits
    (``"  +1.50"``); any other padding character goes between the sign and the
    digits (``"+001.50"``). Non-numeric values are passed through unchanged.
    """

    width: int | None = None
    precision: int | None = None
    force_sign: bool = False
    padding: str = " "

    def __post_init__(self) -> None:
        if len(self.padding) != 1:
            raise ValueError(f"padding must be one character, got {self.padding!r}")

    def format(self, value: Any) -> str:
        number = as_number(value)
        if number is None:
            return str(value)
        if self.precision is not None:
            number = round(number, self.precision)
            digits = f"{abs(number):.{self.precision}f}"
        else:
            digits = str(abs(number))
        sign = "-" if number < 0 else ("+" if self.force_sign else "")
        fill = self.padding * max((self.width or 0) - len(sign) - len(digits), 0)
        if self.padding == " ":
            return f"{fill}{sign}{digits}"
        return f"{sign}{fill}{digits}"


@dataclass(frozen=True)
class Style:
    """ANSI colors and weight for terminal output. Ignored by the web dashboard."""

    fg: str | None = None
    bg: str | None = None
    bold: bool = False
    dim: bool = False
    _prefix: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        codes = []
        if self.bold:
            codes.append("1")
        if self.dim:
            codes.append("2")
        if self.fg is not None:
            codes.append(str(30 + _color(self.fg)))
        if self.bg is not None:
            codes.append(str(40 + _color(self.bg)))
        object.__setattr__(self, "_prefix", f"\x1b[{';'.join(codes)}m" if codes else "")

    def apply(self, text: str) -> str:
        return f"{self._prefix}{text}{RESET}" if self._prefix else text


def styled(text: str, style: Style | None) -> str:
    return style.apply(text) if style else text


def boxed(text: str, width: int, title: str = "") -> str:
    """Draw a box of total ``width`` around ``text``, with an optional title."""
    inner = width - 4
    heading = f" {title} " if title else ""
    top = "┌─" + heading + "─" * max(width - 3 - len(heading), 0) + "┐"
    body = [f"│ {fit(line, inner)} │" for line in text.split("\n")]
    bottom = "└" + "─" * (width - 2) + "┘"
    return "\n".join([top, *body, bottom])


def _color(name: str) -> int:
    try:
        return _COLORS[name.lower()]
    except KeyError:
        raise ValueError(
            f"unknown color {name!r}; expected one of {', '.join(_COLORS)}"
        ) from None
