"""Signals: every accepted value by id, as sent, with its last minute."""

import asyncio
import math
from time import monotonic

import pytest

from sightglass import Monitor, Sparkline, Table
from sightglass.monitor import WRITES_ENV
from sightglass.signals import RESOLUTION, SLOTS, Signals, chart_number, slot_at

from .conftest import Device, until

T = 1000.0  # a monotonic() time at the start of a slot
FIRST = slot_at(T)


def at(slot: int) -> float:
    """A time inside ``slot`` slots after ``T``'s."""
    return T + (slot + 0.5) * RESOLUTION


def last_minute(signals: Signals, id: str, now: float) -> list:
    return signals.snapshot(now)["signals"][id]["trace"]


def test_a_signal_is_its_value_as_sent_with_its_last_minute():
    signals = Signals()
    signals.record([("speed", 2.5)], at(0), version=1)
    snapshot = signals.snapshot(at(3))
    assert snapshot["slot"] == FIRST + 3
    assert snapshot["from"] == FIRST + 3 - SLOTS + 1
    assert snapshot["slots"] == SLOTS
    speed = snapshot["signals"]["speed"]
    assert (speed["text"], speed["number"]) == ("2.5", 2.5)
    assert speed["age"] == pytest.approx(3 * RESOLUTION)
    # Nothing before it arrived; since then, the value holds.
    assert speed["trace"][-5:] == [None, 2.5, 2.5, 2.5, 2.5]
    assert speed["trace"][: SLOTS - 4] == [None] * (SLOTS - 4)


def test_a_slot_spans_every_value_it_saw():
    signals = Signals()
    signals.record([("t", 5), ("t", 9), ("t", 7)], at(0), version=1)
    signals.record([("t", 1)], at(1), version=2)  # from 7, held, down to 1
    signals.record([("t", 1)], at(2), version=3)
    assert last_minute(signals, "t", at(2))[-3:] == [[5, 9], [1, 7], 1]


def test_a_value_that_is_not_a_number_leaves_a_gap():
    signals = Signals()
    signals.record([("v", 3)], at(0), version=1)
    signals.record([("v", "n/a")], at(1), version=2)
    signals.record([("v", "4")], at(3), version=3)  # numeric text counts
    trace = last_minute(signals, "v", at(4))
    assert trace[-5:] == [3, 3, None, 4, 4]
    assert signals.snapshot(at(4))["signals"]["v"]["text"] == "4"


@pytest.mark.parametrize(
    ("value", "number"),
    [
        (True, 1.0),
        (False, 0.0),
        ("on", 1.0),
        (" OFF ", 0.0),
        ("yes", 1.0),
        ("-2.5", -2.5),
        (7, 7.0),
        ("running", None),
        ("", None),
        (None, None),
        ([1, 2], None),
        (math.nan, None),
        (math.inf, None),
        (10**400, None),  # too big for a float
    ],
)
def test_what_charts_as_a_number(value, number):
    assert chart_number(value) == number


def test_a_minute_is_all_that_is_kept():
    signals = Signals()
    for slot in range(SLOTS + 10):
        signals.record([("n", slot)], at(slot), version=slot + 1)
    trace = last_minute(signals, "n", at(SLOTS + 9))
    # Each slot starts from the value before it: one step up, as a band.
    assert trace == [[slot - 1, slot] for slot in range(10, SLOTS + 10)]


def test_a_long_silence_holds_the_last_value_through_the_minute():
    signals = Signals()
    signals.record([("n", 1)], at(0), version=1)
    signals.record([("n", 2)], at(SLOTS * 3), version=2)
    assert last_minute(signals, "n", at(SLOTS * 3)) == [1] * (SLOTS - 1) + [[1, 2]]


def test_changes_are_the_signals_recorded_since_a_version():
    signals = Signals()
    signals.record([("a", 1), ("b", 1)], at(0), version=1)
    signals.record([("b", 2)], at(2), version=2)
    changes = signals.changes(1, FIRST + 1, at(3))
    assert list(changes) == ["b"]
    assert changes["b"]["trace"] == [1, [1, 2], 2]  # slots FIRST+1 to FIRST+3
    assert signals.changes(2, FIRST + 3, at(3)) == {}


def test_the_monitor_records_values_as_sent():
    monitor = Monitor()
    monitor.add(
        Sparkline("rx", scale=1 / 1024),
        Table("axes", rows=["X"], columns=["pos"]),
    )
    monitor.update({"rx": 2048, "axes.X.pos": "1.5", "job.state": "running"})
    monitor.update({"rx": "garbage", "axes.Y.pos": 1})  # rejected: not recorded
    snapshot = monitor.signals.snapshot(monotonic())["signals"]
    assert {id: s["text"] for id, s in snapshot.items()} == {
        "rx": "2048",  # before the element's scale
        "axes.X.pos": "1.5",
        "job.state": "running",
    }
    assert monitor["rx"].text == "2.0"
    assert monitor.signals["rx"].version == monitor.version  # the batch it came in


async def test_values_written_to_a_device_are_not_signals(monkeypatch):
    monkeypatch.setenv(WRITES_ENV, "1")
    monitor, device = Monitor(), Device()
    task = asyncio.create_task(monitor.run(sources=[device]))
    await until(lambda: monitor.claims("dev.setpoint"))
    await monitor.submit({"dev.setpoint": 5, "reading": 4})
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert device.written == [{"dev.setpoint": 5}]
    assert "reading" in monitor.signals
    assert "dev.setpoint" not in monitor.signals  # its reading back will be
