"""Monitor elements: the individual values a dashboard displays.

Every element has an ``id`` that updates are addressed to. An element renders
itself two ways: ``render(width)`` for the terminal and ``to_json()`` for the
web dashboard. Elements with several parts (a table cell, one axis of a
coordinate) also accept updates to a sub-field, addressed as ``"<id>.<field>"``.

To add a new kind of element, subclass :class:`Element` and implement
``update``, ``to_json`` and ``render``.
"""

from __future__ import annotations

import copy
from collections import deque
from collections.abc import Mapping, Sequence
from datetime import datetime
from time import monotonic
from typing import Any, Self

from .formatting import (
    Style,
    TextFormat,
    as_number,
    default_text,
    fit,
    styled,
    visible_len,
)

JSON = Any

NO_VALUE = "-"


class Element:
    """Base class for everything a monitor displays."""

    label_width = 12  # width of the label column in the terminal
    block = False  # multi-line (table, log): the terminal gives it a heading

    def __init__(self, id: str, *, label: str | None = None, border: bool = False):
        if not id:
            raise ValueError("element id must be a non-empty string")
        self.id = id
        self.label = id.rsplit(".", 1)[-1] if label is None else label
        self.border = border

    def update(self, value: Any) -> None:
        """Set the element's value. Raise ValueError/TypeError for bad input."""
        raise NotImplementedError

    def update_field(self, field: str, value: Any) -> None:
        """Set one part of a multi-part element (e.g. ``"row.column"`` of a table)."""
        raise self.unknown_field(field)

    def unknown_field(self, field: str) -> KeyError:
        return KeyError(f"{type(self).__name__} {self.id!r} has no field {field!r}")

    def to_json(self) -> JSON:
        """The value as sent to the web dashboard."""
        raise NotImplementedError

    def render(self, width: int) -> str:
        """The value as drawn in the terminal, at most ``width`` columns wide."""
        raise NotImplementedError

    def describe(self) -> dict[str, JSON]:
        """Static metadata for the web dashboard (sent once per connection)."""
        return {"kind": type(self).__name__, "label": self.label}

    def copy(self, id: str) -> Self:
        """An independent copy of this element with a new id."""
        clone = copy.deepcopy(self)
        clone.id = id
        return clone

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.id!r})"


class TextElement(Element):
    """A single value shown as text, optionally scaled and number-formatted."""

    def __init__(
        self,
        id: str,
        *,
        label: str | None = None,
        value: Any = "",
        prefix: str = "",
        units: str = "",
        scale: float | None = None,
        format: TextFormat | None = None,
        style: Style | None = None,
        border: bool = False,
    ):
        super().__init__(id, label=label, border=border)
        self.prefix = prefix
        self.units = units
        self.scale = scale
        self.format = format
        self.style = style
        self.value = value

    def update(self, value: Any) -> None:
        if self.scale is not None:
            value = float(value) * self.scale
        self.value = value

    @property
    def text(self) -> str:
        return (
            self.format.format(self.value) if self.format else default_text(self.value)
        )

    def to_json(self) -> JSON:
        return self.text

    def render(self, width: int) -> str:
        units = f" {self.units}" if self.units else ""
        value = f"{styled(self.text, self.style)}{units}"
        if self.prefix:  # the caller chose the layout
            return f"{self.prefix}{value}"
        return f"{fit(self.label, self.label_width)} {value}"

    def describe(self) -> dict[str, JSON]:
        return {**super().describe(), "units": self.units}


class ProgressBar(Element):
    """Progress from 0 to ``total``, drawn as a filling bar."""

    def __init__(
        self,
        id: str,
        *,
        total: float = 100,
        label: str | None = None,
        label_width: int = 12,
        bar_style: Style | None = None,
        style: Style | None = None,
        border: bool = False,
    ):
        super().__init__(id, label=label, border=border)
        if total <= 0:
            raise ValueError("total must be positive")
        self.total = total
        self.label_width = label_width
        self.bar_style = bar_style
        self.style = style
        self.value = 0.0

    def update(self, value: Any) -> None:
        self.value = min(max(float(value), 0.0), self.total)

    @property
    def ratio(self) -> float:
        return self.value / self.total

    @property
    def text(self) -> str:
        return f"{self.ratio * 100:.1f}%"

    def to_json(self) -> JSON:
        return {"text": self.text, "ratio": self.ratio}

    def render(self, width: int) -> str:
        text = self.text.rjust(6)
        bar_width = max(width - self.label_width - len(text) - 4, 1)
        filled = int(bar_width * self.ratio)
        bar = "█" * filled + "░" * (bar_width - filled)
        label = fit(self.label, self.label_width)
        return f"{label} [{styled(bar, self.bar_style)}] {styled(text, self.style)}"


class RangeBar(Element):
    """A value within ``[min_value, max_value]``, drawn as a marker on a track.

    The marker is clamped to the track, but the text always shows the actual
    (scaled) value so out-of-range readings are never hidden. Units get a
    column of at least ``units_width`` characters, so bars with different units
    (``"mm/s"``, ``"Nm"``) line up.
    """

    def __init__(
        self,
        id: str,
        *,
        min_value: float = 0,
        max_value: float = 100,
        label: str | None = None,
        units: str = "",
        scale: float = 1,
        precision: int = 2,
        label_width: int = 12,
        value_width: int = 7,
        units_width: int = 4,
        marker: str = "|",
        track: str = "-",
        bar_style: Style | None = None,
        style: Style | None = None,
        border: bool = False,
    ):
        super().__init__(id, label=label, border=border)
        if max_value <= min_value:
            raise ValueError("max_value must be greater than min_value")
        self.min_value = min_value
        self.max_value = max_value
        self.units = units
        self.scale = scale
        self.precision = precision
        self.label_width = label_width
        self.value_width = value_width
        self.units_width = units_width
        self.marker = marker
        self.track = track
        self.bar_style = bar_style
        self.style = style
        self.value: float | None = None

    def update(self, value: Any) -> None:
        self.value = float(value) * self.scale

    @property
    def ratio(self) -> float | None:
        if self.value is None:
            return None
        clamped = min(max(self.value, self.min_value), self.max_value)
        return (clamped - self.min_value) / (self.max_value - self.min_value)

    @property
    def text(self) -> str:
        if self.value is None:
            number = NO_VALUE.rjust(self.value_width)
        else:
            number = f"{self.value:>{self.value_width}.{self.precision}f}"
        return f"{number} {self.units}" if self.units else number

    def to_json(self) -> JSON:
        return {"text": self.text.strip(), "ratio": self.ratio}

    def render(self, width: int) -> str:
        text_width = self.value_width
        if self.units:
            text_width += 1 + max(len(self.units), self.units_width)
        bar_width = max(width - self.label_width - text_width - 4, 1)
        if self.ratio is None:
            bar = self.track * bar_width
        else:
            position = min(int(bar_width * self.ratio), bar_width - 1)
            bar = (
                self.track * position
                + self.marker
                + self.track * (bar_width - position - 1)
            )
        label = fit(self.label, self.label_width)
        readout = styled(self.text, self.style) + " " * (text_width - len(self.text))
        return f"{label} [{styled(bar, self.bar_style)}] {readout}"

    def describe(self) -> dict[str, JSON]:
        return {
            **super().describe(),
            "units": self.units,
            "min": self.min_value,
            "max": self.max_value,
        }


SPARK = "▁▂▃▄▅▆▇█"


class Sparkline(Element):
    """A number with a small chart of its last ``points`` values, for spotting
    trends (throughput, temperature, error rate). Values are multiplied by
    ``scale`` (e.g. ``1 / 1024`` to show bytes as KB).

    With ``interval``, the chart keeps one point per ``interval`` seconds:
    updates within an interval replace its point, so a value sent many times a
    second can chart a longer period (``points * interval`` seconds). The
    number shown is always the latest value.
    """

    def __init__(
        self,
        id: str,
        *,
        label: str | None = None,
        points: int = 60,
        interval: float | None = None,
        units: str = "",
        scale: float = 1,
        format: TextFormat | None = None,
        label_width: int = 12,
        style: Style | None = None,
        border: bool = False,
    ):
        super().__init__(id, label=label, border=border)
        if points < 2:
            raise ValueError("points must be at least 2")
        if interval is not None and interval <= 0:
            raise ValueError("interval must be positive")
        self.interval = interval
        self.units = units
        self.scale = scale
        self.format = format
        self.label_width = label_width
        self.style = style
        self.history: deque[float] = deque(maxlen=points)
        self._point_started = 0.0

    def update(self, value: Any) -> None:
        number = as_number(value)
        if number is None:
            raise ValueError("not a number")
        point = float(number) * self.scale
        if self.interval is not None:
            now = monotonic()
            if self.history and now - self._point_started < self.interval:
                self.history[-1] = point
                return
            self._point_started = now
        self.history.append(point)

    @property
    def text(self) -> str:
        if not self.history:
            return NO_VALUE
        latest = self.history[-1]
        text = self.format.format(latest) if self.format else default_text(latest)
        return f"{text} {self.units}" if self.units else text

    def to_json(self) -> JSON:
        return {"text": self.text, "values": list(self.history)}

    def render(self, width: int) -> str:
        chart_width = max(width - self.label_width - 2 - len(self.text), 1)
        values = list(self.history)[-chart_width:]
        if values:
            low, high = min(values), max(values)
            if high == low:  # steady: a level line mid-height, not one at zero
                chart = SPARK[len(SPARK) // 2 - 1] * len(values)
            else:
                chart = "".join(
                    SPARK[round((v - low) / (high - low) * (len(SPARK) - 1))]
                    for v in values
                )
        else:
            chart = ""
        label = fit(self.label, self.label_width)
        return f"{label} {chart.ljust(chart_width)} {styled(self.text, self.style)}"

    def describe(self) -> dict[str, JSON]:
        return {**super().describe(), "units": self.units}


class Table(Element):
    """A grid of values. Cells are addressed as ``"<id>.<row>.<column>"``."""

    block = True

    def __init__(
        self,
        id: str,
        *,
        rows: Sequence[str],
        columns: Sequence[str],
        label: str | None = None,
        cell_width: int = 8,
        row_label_width: int | None = None,
        format: TextFormat | None = None,
        border: bool = False,
    ):
        super().__init__(id, label=label, border=border)
        self.rows = list(rows)
        self.columns = list(columns)
        self.cell_width = cell_width
        self.row_label_width = row_label_width or max(map(len, self.rows), default=1)
        self.format = format
        self.values: dict[str, dict[str, Any]] = {
            row: dict.fromkeys(self.columns, "") for row in self.rows
        }

    def update(self, value: Any) -> None:
        if not isinstance(value, Mapping):
            raise TypeError("Table.update expects a mapping of {row: {column: value}}")
        for row, cells in value.items():
            for column, cell in cells.items():
                self.update_field(f"{row}.{column}", cell)

    def update_field(self, field: str, value: Any) -> None:
        row, _, column = field.partition(".")
        if row not in self.values or column not in self.values[row]:
            raise self.unknown_field(field)
        self.values[row][column] = value

    def _cell(self, value: Any) -> str:
        return self.format.format(value) if self.format else default_text(value)

    def to_json(self) -> JSON:
        return {
            row: {column: self._cell(value) for column, value in cells.items()}
            for row, cells in self.values.items()
        }

    def render(self, width: int) -> str:
        widths = [self.row_label_width + 2] + [self.cell_width + 2] * len(self.columns)

        def rule(left: str, middle: str, right: str) -> str:
            return left + middle.join("─" * w for w in widths) + right

        def line(first: str, cells: list[str]) -> str:
            first = " " + fit(first, self.row_label_width) + " "
            rest = [
                " " + fit(c.center(self.cell_width), self.cell_width) + " "
                for c in cells
            ]
            return "│" + "│".join([first, *rest]) + "│"

        lines = [rule("┌", "┬", "┐"), line("", self.columns), rule("├", "┼", "┤")]
        for row in self.rows:
            lines.append(
                line(row, [self._cell(self.values[row][c]) for c in self.columns])
            )
        lines.append(rule("└", "┴", "┘"))
        return "\n".join(lines)

    def describe(self) -> dict[str, JSON]:
        return {**super().describe(), "rows": self.rows, "columns": self.columns}


class LogMonitor(Element):
    """The most recent ``lines`` messages, newest last."""

    block = True

    def __init__(
        self,
        id: str,
        *,
        lines: int = 10,
        timestamp: bool = False,
        timestamp_format: str = "%H:%M:%S",
        label: str | None = None,
        border: bool = False,
    ):
        super().__init__(id, label=label, border=border)
        self.lines = lines
        self.timestamp = timestamp
        self.timestamp_format = timestamp_format
        self.entries: deque[str] = deque(maxlen=lines)

    def update(self, value: Any) -> None:
        message = str(value)
        if self.timestamp:
            message = f"{datetime.now().strftime(self.timestamp_format)} {message}"
        self.entries.append(message)

    def to_json(self) -> JSON:
        return "\n".join(self.entries)

    def render(self, width: int) -> str:
        padded = [*self.entries, *[""] * (self.lines - len(self.entries))]
        return "\n".join(fit(entry, width) for entry in padded)


def parse_bool(value: Any) -> bool:
    """Interpret device-style truthy values: 1/0, true/false, on/off, yes/no."""
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "on", "yes"):
            return True
        if text in ("0", "false", "off", "no", ""):
            return False
        raise ValueError(f"not a boolean: {value!r}")
    return bool(value)


LAMP = "●"


class IndicatorLamp(Element):
    """An on/off indicator."""

    def __init__(
        self,
        id: str,
        *,
        label: str | None = None,
        on_color: str = "green",
        off_color: str = "red",
        border: bool = False,
    ):
        super().__init__(id, label=label, border=border)
        self.on_style = Style(fg=on_color, bold=True)
        self.off_style = Style(fg=off_color, bold=True)
        self.value = False

    def update(self, value: Any) -> None:
        self.value = parse_bool(value)

    def to_json(self) -> JSON:
        return self.value

    def render(self, width: int) -> str:
        style = self.on_style if self.value else self.off_style
        return f"{fit(self.label, self.label_width)} {style.apply(LAMP)}"


def parse_int(value: Any) -> int:
    """Parse an integer, accepting ``0x``/``0b`` prefixed strings."""
    if isinstance(value, str) and value.strip()[:2].lower() in ("0x", "0b"):
        return int(value.strip(), 0)
    return int(value)


class MachineState(Element):
    """Named on/off states packed into the bits of one integer.

    ``states[0]`` is bit 0 (least significant). The whole word is updated with
    an integer; a single state can be set via ``"<id>.<state>"``.
    """

    def __init__(
        self,
        id: str,
        *,
        states: Sequence[str],
        label: str | None = None,
        on_color: str = "green",
        off_color: str = "red",
        border: bool = False,
    ):
        super().__init__(id, label=label, border=border)
        self.states = list(states)
        self.on_style = Style(fg=on_color, bold=True)
        self.off_style = Style(fg=off_color, bold=True)
        self.bits = 0

    def update(self, value: Any) -> None:
        self.bits = parse_int(value)

    def update_field(self, field: str, value: Any) -> None:
        if field not in self.states:
            raise self.unknown_field(field)
        mask = 1 << self.states.index(field)
        self.bits = self.bits | mask if parse_bool(value) else self.bits & ~mask

    def as_dict(self) -> dict[str, bool]:
        return {state: bool(self.bits >> i & 1) for i, state in enumerate(self.states)}

    def to_json(self) -> JSON:
        return self.as_dict()

    def render(self, width: int) -> str:
        indent = self.label_width + 1
        lines, line = [], ""
        for state, on in self.as_dict().items():
            lamp = (self.on_style if on else self.off_style).apply(LAMP)
            item = f"{lamp} {state}"
            if line and indent + visible_len(line) + 2 + visible_len(item) > width:
                lines.append(line)
                line = ""
            line = f"{line}  {item}" if line else item
        lines.append(line)
        label = fit(self.label, self.label_width)
        return "\n".join(
            f"{label if i == 0 else ' ' * self.label_width} {text}"
            for i, text in enumerate(lines)
        )

    def describe(self) -> dict[str, JSON]:
        return {**super().describe(), "states": self.states}


class Coordinate(Element):
    """A multi-axis position, e.g. X/Y/Z. Axes are fields: ``"<id>.x"``."""

    def __init__(
        self,
        id: str,
        *,
        axes: Sequence[str] = ("x", "y", "z"),
        label: str | None = None,
        units: str = "",
        format: TextFormat | None = None,
        style: Style | None = None,
        border: bool = False,
    ):
        super().__init__(id, label=label, border=border)
        self.axes = list(axes)
        self.units = units
        self.format = format or TextFormat(precision=4, force_sign=True)
        self.style = style
        self.values: dict[str, Any] = dict.fromkeys(self.axes)

    def update(self, value: Any) -> None:
        if isinstance(value, Mapping):
            for axis, v in value.items():
                self.update_field(axis, v)
        else:
            values = list(value)
            if len(values) != len(self.axes):
                raise ValueError(f"expected {len(self.axes)} values, got {len(values)}")
            self.values.update(zip(self.axes, values, strict=True))

    def update_field(self, field: str, value: Any) -> None:
        if field not in self.values:
            raise self.unknown_field(field)
        self.values[field] = value

    def _text(self, axis: str) -> str:
        value = self.values[axis]
        return NO_VALUE if value is None else self.format.format(value)

    def to_json(self) -> JSON:
        return {axis: self._text(axis) for axis in self.axes}

    def render(self, width: int) -> str:
        units = f" {self.units}" if self.units else ""
        parts = [
            f"{axis.upper()}: {styled(self._text(axis), self.style)}{units}"
            for axis in self.axes
        ]
        return f"{self.label} " + " ".join(parts)

    def describe(self) -> dict[str, JSON]:
        return {**super().describe(), "units": self.units, "axes": self.axes}
