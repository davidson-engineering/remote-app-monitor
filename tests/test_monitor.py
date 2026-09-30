import asyncio
import logging
import subprocess
import sys
import threading
import time

import pytest

from app_monitor import (
    Group,
    IndicatorLamp,
    MachineState,
    Monitor,
    RangeBar,
    SimulatedSource,
    Table,
    TextElement,
)


def make_monitor(**options) -> Monitor:
    monitor = Monitor(**options)
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


def test_duplicate_ids_and_groups_are_rejected():
    monitor = Monitor()
    monitor.add(TextElement("a"))
    with pytest.raises(ValueError, match="duplicate element id 'a'"):
        monitor.add(TextElement("b"), TextElement("a"))
    assert "b" not in monitor  # nothing from a rejected call is added
    monitor.add_group("G", [TextElement("x")])
    with pytest.raises(ValueError, match="duplicate group 'G'"):
        monitor.add_group("G", [TextElement("y")])


def test_fields_route_to_their_element():
    monitor = make_monitor()
    monitor.set("t.a.x", 7)
    assert monitor["t"].to_json() == {"a": {"x": "7"}}


# -- elements created on first use ------------------------------------------------


def test_unknown_ids_create_elements():
    monitor = Monitor()
    monitor.update({"rate": 18.627682319492283, "pump": True, "status": "running"})
    assert isinstance(monitor["rate"], TextElement)
    assert monitor["rate"].to_json() == "18.6277"
    assert isinstance(monitor["pump"], IndicatorLamp)
    assert monitor["pump"].to_json() is True
    assert [item.id for item in monitor.layout] == ["rate", "pump", "status"]


def test_dotted_ids_are_grouped_by_prefix():
    monitor = Monitor()
    monitor.update({"job.progress": 5, "temp": 20, "job.rate": 1.5})
    job = monitor.layout[0]
    assert isinstance(job, Group) and job.name == "job"
    assert [el.id for el in job.elements] == ["job.progress", "job.rate"]
    assert [el.label for el in job.elements] == ["progress", "rate"]
    assert monitor.layout[1].id == "temp"


def test_new_elements_change_the_layout_version():
    monitor = Monitor()
    monitor.set("a", 1)
    layout = monitor.layout_version
    monitor.set("a", 2)
    assert monitor.layout_version == layout  # same element: no layout change
    monitor.set("b", 1)
    assert monitor.layout_version == layout + 1


def test_strict_monitor_rejects_unknown_ids_and_reports_once(caplog):
    monitor = make_monitor(strict=True)
    with caplog.at_level(logging.WARNING, logger="app_monitor"):
        monitor.update({"nope": 1, "X.velocity": "fast", "speed": "ok"})
        monitor.update({"nope": 2})
        monitor.update({"t.b.x": 1})
    assert "nope" not in monitor
    assert monitor["speed"].value == "ok"
    assert monitor.rejected == 4
    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 3  # the repeat of "nope" is logged at DEBUG
    assert "'nope'" in messages[0] and "no element" in messages[0]
    assert "'X.velocity'" in messages[1] and "bad value 'fast'" in messages[1]
    assert "'t.b.x'" in messages[2]


def test_element_limit_stops_runaway_creation(caplog):
    monitor = Monitor(max_elements=2)
    with caplog.at_level(logging.WARNING, logger="app_monitor"):
        monitor.update({"a": 1, "b": 2, "c": 3})
    assert len(monitor) == 2
    assert "max_elements" in caplog.text


def test_warnings_are_visible_without_logging_setup():
    """The library must not hide its warnings behind a NullHandler."""
    script = (
        "from app_monitor import Monitor\nm = Monitor(strict=True)\nm.set('typo', 1)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    assert "Ignored update for 'typo'" in result.stderr


# -- change tracking ----------------------------------------------------------------


def test_versions_track_what_changed():
    monitor = make_monitor()
    start = monitor.version
    monitor.update({"speed": 1}, {"X.velocity": 2})
    assert monitor.version == start + 1  # one batch, one version
    monitor.update({"speed": 3})
    assert monitor.changes_since(start + 1) == {"speed": "3"}
    assert set(monitor.changes_since(start)) == {"speed", "X.velocity"}
    monitor.update({"X.velocity": "fast"})
    assert monitor.version == start + 2  # rejected updates don't count as changes


def test_ages_say_how_long_ago_each_element_was_updated(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("app_monitor.monitor.monotonic", lambda: now[0])
    monitor = make_monitor()
    monitor.update({"speed": 1})
    now[0] = 102.5
    monitor.update({"X.velocity": 2}, {"X.torque": "fast"})  # torque is rejected
    now[0] = 103.0
    ages = monitor.ages()
    assert ages["speed"] == 3.0
    assert ages["X.velocity"] == 0.5
    assert ages["X.torque"] is None  # never updated
    monitor.update({"speed": 1})  # the same value again still counts
    assert monitor.ages()["speed"] == 0.0


async def test_wait_for_change():
    monitor = make_monitor()
    now = monitor.version
    waiter = asyncio.create_task(monitor.wait_for_change(now))
    await asyncio.sleep(0)
    assert not waiter.done()
    monitor.set("speed", 1)
    assert await asyncio.wait_for(waiter, 1) == now + 1
    assert await monitor.wait_for_change(-1) == now + 1  # already different


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


# -- threads ----------------------------------------------------------------------


async def test_updates_from_another_thread_wake_the_loop_promptly():
    """The original bug: set() from the app's thread only showed up when
    something else (a 10 s heartbeat) happened to wake the event loop."""
    monitor = make_monitor()
    version = monitor.version
    await monitor.wait_for_change(-1)  # binds the monitor to this loop

    def app():
        time.sleep(0.05)
        monitor.set("speed", "from thread")

    threading.Thread(target=app).start()
    started = time.monotonic()
    await asyncio.wait_for(monitor.wait_for_change(version), 2)
    assert time.monotonic() - started < 0.5
    assert monitor["speed"].value == "from thread"


async def test_updates_from_threads_keep_their_order():
    monitor = Monitor()
    monitor.add(TextElement("n"))
    await monitor.wait_for_change(-1)
    seen, all_seen = [], asyncio.Event()

    def record(value):  # replaces the element's update(): runs on the loop
        seen.append(value)
        if len(seen) == 500:
            all_seen.set()

    monitor["n"].update = record

    def app():
        for i in range(500):
            monitor.set("n", i)

    await asyncio.to_thread(app)
    await asyncio.wait_for(all_seen.wait(), 5)
    assert seen == list(range(500))


async def test_add_from_another_thread_waits_and_reports_errors():
    monitor = Monitor()
    monitor.add(TextElement("a"))
    await monitor.wait_for_change(-1)

    def app():
        monitor.add(TextElement("b"))
        with pytest.raises(ValueError, match="duplicate"):
            monitor.add(TextElement("a"))

    await asyncio.to_thread(app)
    assert "b" in monitor


# -- running ------------------------------------------------------------------------


async def test_run_feeds_sources_until_cancelled():
    monitor = make_monitor()
    source = SimulatedSource(lambda t: {"speed": "tick"}, rate=100)
    version = monitor.version
    task = asyncio.create_task(monitor.run(sources=[source]))
    await asyncio.wait_for(monitor.wait_for_change(version), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert monitor["speed"].value == "tick"


async def test_run_raises_a_single_failure_directly():
    class Broken:
        async def updates(self):
            raise OSError("device unplugged")
            yield  # pragma: no cover

    with pytest.raises(OSError, match="device unplugged"):
        await Monitor().run(sources=[Broken()])


class Recorder:
    """An output that records its lifecycle."""

    def __init__(self, fail_start=False):
        self.fail_start = fail_start
        self.events = []

    async def start(self, monitor):
        if self.fail_start:
            raise OSError("port in use")
        self.events.append("start")

    async def run(self, monitor):
        self.events.append("run")
        try:
            await asyncio.Event().wait()
        finally:
            self.events.append("end")

    async def close(self):
        self.events.append("close")


def test_start_runs_in_the_background_and_stop_shuts_down():
    output = Recorder()
    monitor = Monitor().start(outputs=[output])
    assert output.events[0] == "start"  # outputs are up when start() returns
    monitor.set("progress", 3)  # from the main thread
    deadline = time.monotonic() + 2
    while "progress" not in monitor:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    with pytest.raises(RuntimeError, match="already running"):
        monitor.start(outputs=[])
    monitor.stop()
    assert output.events == ["start", "run", "end"]
    monitor.stop()  # harmless when already stopped


def test_start_raises_setup_errors_and_closes_what_started():
    first, second = Recorder(), Recorder(fail_start=True)
    with pytest.raises(OSError, match="port in use"):
        Monitor().start(outputs=[first, second])
    assert first.events == ["start", "close"]


def test_monitor_is_a_context_manager():
    output = Recorder()
    with Monitor().start(outputs=[output]) as monitor:
        monitor.set("a", 1)
    assert output.events[-1] == "end"


def test_serve_returns_quietly_on_ctrl_c():
    script = (
        "import os, signal, threading\n"
        "from app_monitor import Monitor, SimulatedSource\n"
        "threading.Timer(0.5, os.kill, (os.getpid(), signal.SIGINT)).start()\n"
        "Monitor().serve(sources=[SimulatedSource(lambda t: {'t': t})], outputs=[])\n"
        "print('returned')\n"
    )
    if sys.platform == "win32":
        pytest.skip("SIGINT delivery differs on Windows")
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "returned"
    assert "Traceback" not in result.stderr
