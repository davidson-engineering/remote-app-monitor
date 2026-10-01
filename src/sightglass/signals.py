"""Every value a monitor has accepted, by id, with its last minute: what the
dashboard's signals panel shows, for checking what a feed actually sends.

A signal is an id as it was sent (``"X.velocity"``, ``"table.row.column"``)
and its value as sent, before an element scales or formats it. History is
kept in time slots shared by every signal, so their charts line up: each slot
holds the lowest and highest value the signal had during it, and a value that
didn't change carries on through the slots that follow. Values that aren't
numbers have no history; true/false (and on/off, yes/no) chart as 1/0.
"""

from __future__ import annotations

import math
import numbers
from array import array
from collections.abc import Iterable
from typing import Any

from .formatting import as_number, default_text

RESOLUTION = 0.5
"""Seconds per slot."""

SLOTS = 120
"""Slots kept per signal: a minute."""

Point = float | list[float] | None
"""One slot as sent to the page: the value, ``[lowest, highest]`` when it
varied, or None for no number."""

_SWITCHES = {"true": 1.0, "on": 1.0, "yes": 1.0, "false": 0.0, "off": 0.0, "no": 0.0}


def chart_number(value: Any) -> float | None:
    """``value`` as a point on a chart, or None if it isn't a finite number
    (or true/false, on/off, yes/no: 1/0)."""
    if type(value) is float:  # the usual case, first
        return value if math.isfinite(value) else None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        number = as_number(value)
        if number is None:
            return _SWITCHES.get(value.strip().lower())
    elif isinstance(value, numbers.Real):  # NumPy's numbers too
        number = value
    else:
        return None
    try:
        number = float(number)
    except OverflowError:  # an int too big for a float
        return None
    return number if math.isfinite(number) else None


def slot_at(now: float) -> int:
    """The slot that ``now`` (a ``monotonic()`` time) falls in."""
    return math.floor(now / RESOLUTION)


class Signal:
    """One id's latest value and its last :data:`SLOTS` slots."""

    __slots__ = ("_high", "_low", "number", "slot", "updated", "value", "version")

    def __init__(self, slot: int) -> None:
        self.value: Any = None
        self.number: float | None = None  # the latest value, charted
        self.updated = 0.0
        self.version = 0
        self.slot = slot  # the newest slot started
        self._low = array("d", [math.nan]) * SLOTS
        self._high = array("d", [math.nan]) * SLOTS

    def record(self, value: Any, slot: int, now: float, version: int) -> None:
        """``value`` arrived at ``now``, in ``slot`` (:func:`slot_at`)."""
        if slot != self.slot:
            self._start(slot)
        number = chart_number(value)
        if number is not None:
            i = slot % SLOTS
            low = self._low[i]
            if math.isnan(low):
                self._low[i] = self._high[i] = number
            elif number < low:
                self._low[i] = number
            elif number > self._high[i]:
                self._high[i] = number
        self.value, self.number = value, number
        self.updated, self.version = now, version

    def _start(self, slot: int) -> None:
        """Start the slots up to ``slot``, each from the value held before it."""
        held = math.nan if self.number is None else self.number
        for s in range(max(self.slot + 1, slot - SLOTS + 1), slot + 1):
            self._low[s % SLOTS] = self._high[s % SLOTS] = held
        self.slot = max(self.slot, slot)

    def trace(self, first: int, last: int) -> list[Point]:
        """Slots ``first`` to ``last``, inclusive."""
        held = self.number
        points: list[Point] = []
        for s in range(first, last + 1):
            if s > self.slot:  # no value since: the latest holds
                points.append(held)
                continue
            if s <= self.slot - SLOTS:  # forgotten
                points.append(None)
                continue
            low, high = self._low[s % SLOTS], self._high[s % SLOTS]
            if math.isnan(low):
                points.append(None)
            else:
                points.append(low if low == high else [low, high])
        return points

    def to_json(self, first: int, last: int, now: float) -> dict[str, Any]:
        return {
            "text": default_text(self.value),
            "number": self.number,
            "age": max(now - self.updated, 0.0),
            "trace": self.trace(first, last),
        }


class Signals:
    """The signals a monitor has accepted (:attr:`Monitor.signals`), recorded
    on its event loop's thread."""

    def __init__(self) -> None:
        self._signals: dict[str, Signal] = {}

    def __len__(self) -> int:
        return len(self._signals)

    def __contains__(self, id: object) -> bool:
        return id in self._signals

    def __getitem__(self, id: str) -> Signal:
        return self._signals[id]

    def record(
        self, values: Iterable[tuple[str, Any]], now: float, version: int
    ) -> None:
        """Record ``(id, value)`` pairs that arrived at ``now`` (``monotonic()``),
        in the monitor's ``version``."""
        slot = slot_at(now)
        for id, value in values:
            signal = self._signals.get(id)
            if signal is None:
                signal = self._signals[id] = Signal(slot - 1)
            signal.record(value, slot, now, version)

    def snapshot(self, now: float) -> dict[str, Any]:
        """Every signal with its last minute, ending with the slot of ``now``."""
        last = slot_at(now)
        first = last - SLOTS + 1
        return {
            "resolution": RESOLUTION,
            "slots": SLOTS,
            "slot": last,
            "from": first,
            "signals": {
                id: signal.to_json(first, last, now)
                for id, signal in self._signals.items()
            },
        }

    def changes(self, version: int, first: int, now: float) -> dict[str, Any]:
        """The signals recorded after ``version``, with slots ``first`` to the
        slot of ``now``."""
        last = slot_at(now)
        return {
            id: signal.to_json(first, last, now)
            for id, signal in self._signals.items()
            if signal.version > version
        }
