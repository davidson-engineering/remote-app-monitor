import asyncio
import logging

import pytest

from app_monitor import (
    MachineState,
    Monitor,
    RangeBar,
    SimulatedSource,
    Table,
    TextElement,
)


def make_monitor() -> Monitor:
    monitor = Monitor()
    monitor.add(TextElement("speed"), Table("t", rows=["a"], columns=["x"]))
    monitor.add_group("X", [RangeBar("velocity"), RangeBar("torque")])
    monitor.add_group("Y", [RangeBar("velocity"), RangeBar("torque")])
    return monitor


def test_groups_prefix_ids_and_copy_elements():
    monitor = make_monitor()
    assert [el.id for el in monitor] == [
        "speed",
        "t",
        "X.velocity",
        "X.torque",
        "Y.velocity",
        "Y.torque",
    ]
    monitor.update({"X.velocity": 10, "Y.velocity": 20})
    assert monitor["X.velocity"].value == 10
    assert monitor["Y.velocity"].value == 20


def test_duplicate_ids_are_rejected():
    monitor = Monitor()
    monitor.add(TextElement("a"))
    with pytest.raises(ValueError, match="duplicate"):
        monitor.add(TextElement("a"))


def test_fields_route_to_their_element():
    monitor = make_monitor()
    monitor.set("t.a.x", 7)
    assert monitor["t"].to_json() == {"a": {"x": "7"}}


def test_bad_updates_are_skipped_and_reported_once(caplog):
    monitor = make_monitor()
    with caplog.at_level(logging.WARNING, logger="app_monitor"):
        monitor.update({"nope": 1, "X.velocity": "fast", "speed": "ok"})
        monitor.update({"nope": 2})
        monitor.update({"t.b.x": 1})
    assert monitor["speed"].value == "ok"
    assert monitor.rejected == 4
    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 3  # the repeat of "nope" is logged at DEBUG
    assert "'nope'" in messages[0] and "no element" in messages[0]
    assert "'X.velocity'" in messages[1] and "bad value 'fast'" in messages[1]
    assert "'t.b.x'" in messages[2]


def test_versions_track_what_changed():
    monitor = make_monitor()
    assert monitor.version == 0
    monitor.update({"speed": 1}, {"X.velocity": 2})
    assert monitor.version == 1  # one batch, one version
    monitor.update({"speed": 3})
    assert monitor.changes_since(1) == {"speed": "3"}
    assert set(monitor.changes_since(0)) == {"speed", "X.velocity"}
    monitor.update({"nope": 1})
    assert monitor.version == 2  # rejected updates don't count as changes


async def test_wait_for_change():
    monitor = make_monitor()
    waiter = asyncio.create_task(monitor.wait_for_change(0))
    await asyncio.sleep(0)
    assert not waiter.done()
    monitor.set("speed", 1)
    assert await asyncio.wait_for(waiter, 1) == 1
    assert await monitor.wait_for_change(-1) == 1  # already different: no wait


def test_describe():
    monitor = Monitor()
    monitor.add(MachineState("m", states=["on"]))
    monitor.add_group("X", [RangeBar("v", units="mm")])
    assert monitor.describe() == [
        {"id": "m", "kind": "MachineState", "label": "m", "states": ["on"]},
        {
            "group": "X",
            "elements": [
                {
                    "id": "X.v",
                    "kind": "RangeBar",
                    "label": "v",
                    "units": "mm",
                    "min": 0,
                    "max": 100,
                }
            ],
        },
    ]


async def test_run_feeds_sources_until_cancelled():
    monitor = make_monitor()
    source = SimulatedSource(lambda t: {"speed": "tick"}, rate=100)
    task = asyncio.create_task(monitor.run(sources=[source]))
    await asyncio.wait_for(monitor.wait_for_change(0), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert monitor["speed"].value == "tick"
