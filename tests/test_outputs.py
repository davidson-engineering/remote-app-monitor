import asyncio
import io
import os

import pytest
from aiohttp.test_utils import TestClient, TestServer

from sightglass import (
    MachineState,
    Monitor,
    ProgressBar,
    RangeBar,
    TerminalDisplay,
    TextElement,
    WebDashboard,
)


def make_monitor() -> Monitor:
    monitor = Monitor()
    monitor.add(TextElement("speed", prefix="Speed: "))
    monitor.add_group("X", [RangeBar("velocity", units="mm/s")])
    monitor.add(ProgressBar("job", border=True))
    monitor.add(MachineState("machine", states=["estop", "enabled"]))
    return monitor


# -- terminal -----------------------------------------------------------------


def test_terminal_frame(monkeypatch):
    monkeypatch.setattr("shutil.get_terminal_size", lambda: os.terminal_size((120, 40)))
    monitor = make_monitor()
    monitor.update({"speed": 5, "X.velocity": 50, "job": 25})
    frame = TerminalDisplay(width=40).render(monitor).split("\n")
    assert frame[0] == "Speed: 5"
    assert frame[1] == "┌─ X ──────────────────────────────────┐"
    assert frame[2].startswith(f"│ {'velocity':<12} [")
    assert frame[2].endswith("50.00 mm/s │")
    assert frame[4] == "┌─ job ────────────────────────────────┐"
    assert "25.0%" in frame[5]
    assert all(len(line) == 40 for line in frame[1:7])


def test_terminal_width_follows_a_narrow_terminal(monkeypatch):
    monkeypatch.setattr("shutil.get_terminal_size", lambda: os.terminal_size((30, 40)))
    frame = TerminalDisplay(width=60).render(make_monitor()).split("\n")
    assert len(frame[1]) == 30


def test_terminal_screen_draws_the_whole_terminal(monkeypatch):
    """A screen function lays out the frame itself, like a custom web page."""
    monkeypatch.setattr("shutil.get_terminal_size", lambda: os.terminal_size((100, 3)))
    calls = []

    def screen(monitor, width, height):
        calls.append((width, height))
        return "\n".join(f"line {i}: {monitor['speed'].text}" for i in range(5))

    monitor = make_monitor()
    monitor.set("speed", 7)
    frame = TerminalDisplay(screen=screen).render(monitor)
    assert calls == [(100, 3)]  # the whole terminal, not the 60-column default
    assert frame.split("\n") == ["line 0: 7", "line 1: 7", "line 2: 7"]  # fits


async def test_terminal_screen_redraws_without_changes():
    """A screen may show time (how old data is), so it's redrawn regularly."""
    frames = []
    display = TerminalDisplay(
        screen=lambda monitor, width, height: str(len(frames)), stream=io.StringIO()
    )
    display.refresh = 0.05
    display.render = lambda monitor: frames.append(1) or "frame"
    task = asyncio.create_task(display.run(make_monitor()))
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(frames) >= 4  # nothing changed, yet it kept drawing


async def test_terminal_redraws_on_change_and_restores_screen():
    monitor = make_monitor()
    out = io.StringIO()
    task = asyncio.create_task(TerminalDisplay(fps=1000, stream=out).run(monitor))
    await asyncio.sleep(0.05)
    assert out.getvalue().startswith("\x1b[?1049h\x1b[?25l\x1b[H")
    frames = out.getvalue().count("\x1b[H")
    await asyncio.sleep(0.05)
    assert out.getvalue().count("\x1b[H") == frames  # idle: no redraw
    monitor.set("speed", 7)
    await asyncio.sleep(0.05)
    assert out.getvalue().count("\x1b[H") == frames + 1
    assert "Speed: 7" in out.getvalue()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    restored = out.getvalue().rsplit("\x1b[?25h\x1b[?1049l", 1)
    assert len(restored) == 2  # cursor shown, normal screen back
    assert restored[1].startswith("Speed: 7")  # then the final frame, kept in view


# -- web ------------------------------------------------------------------------


@pytest.fixture
async def client():
    monitor = make_monitor()
    dashboard = WebDashboard(fps=200, title="Line 3", stale_after=2.5)
    async with TestClient(TestServer(dashboard.app(monitor))) as client:
        client.monitor = monitor
        yield client


async def test_web_serves_page_and_assets(client):
    page = await client.get("/")
    assert page.status == 200
    assert "/_sightglass/sightglass.js" in await page.text()
    for asset in ["sightglass.js", "panel.css", "fonts/DSEG14Modern-Regular.ttf"]:
        response = await client.get(f"/_sightglass/{asset}")
        assert response.status == 200, asset
        assert response.headers["Cache-Control"] == "no-cache"


async def test_web_sends_snapshot_then_only_changes(client):
    ws = await client.ws_connect("/ws")
    snapshot = await ws.receive_json(timeout=1)
    assert snapshot["type"] == "snapshot"
    assert (snapshot["title"], snapshot["staleAfter"]) == ("Line 3", 2.5)
    assert snapshot["values"]["machine"] == {"estop": False, "enabled": False}
    assert snapshot["ages"]["machine"] is None  # never updated
    assert [item.get("id", item.get("group")) for item in snapshot["layout"]] == [
        "speed",
        "X",
        "job",
        "machine",
    ]

    client.monitor.update({"speed": 3, "machine": 2})
    update = await ws.receive_json(timeout=1)
    assert update == {
        "type": "update",
        "values": {"speed": "3", "machine": {"estop": False, "enabled": True}},
    }
    client.monitor.update({"X.velocity": 12})
    update = await ws.receive_json(timeout=1)
    assert update["values"] == {"X.velocity": {"text": "12.00 mm/s", "ratio": 0.12}}
    await ws.close()


async def test_web_custom_page_and_static_dir(tmp_path):
    (tmp_path / "page.html").write_text("<p>custom</p>")
    (tmp_path / "static").mkdir()
    (tmp_path / "static" / "site.css").write_text("p{}")
    dashboard = WebDashboard(tmp_path / "page.html", static_dir=tmp_path / "static")
    async with TestClient(TestServer(dashboard.app(Monitor()))) as client:
        assert await (await client.get("/")).text() == "<p>custom</p>"
        assert (await client.get("/static/site.css")).status == 200


def test_web_rejects_missing_page(tmp_path):
    with pytest.raises(FileNotFoundError):
        WebDashboard(tmp_path / "nope.html")


async def test_web_resends_layout_when_elements_appear(client):
    ws = await client.ws_connect("/ws")
    await ws.receive_json(timeout=1)
    client.monitor.set("new.thing", 5)  # created on first use
    message = await ws.receive_json(timeout=1)
    assert message["type"] == "snapshot"
    assert message["layout"][-1] == {
        "group": "new",
        "elements": [
            {"id": "new.thing", "kind": "TextElement", "label": "thing", "units": ""}
        ],
    }
    assert message["values"]["new.thing"] == "5"
    await ws.close()


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b'{"speed": 4, "X": {"velocity": 50}}', {"speed": 4, "X.velocity": 50.0}),
        (b'[{"speed": 1}, {"speed": 2}]', {"speed": 2}),
        (b"speed=5 new=hello", {"speed": "5", "new": "hello"}),
        (b"speed 6\nlog motor 2 stalled\n", {"speed": "6", "log": "motor 2 stalled"}),
    ],
)
async def test_web_accepts_posted_updates(client, body, expected):
    response = await client.post("/update", data=body)
    assert response.status == 204
    for id, value in expected.items():
        assert client.monitor[id].value == value


@pytest.mark.parametrize("body", [b"", b"{nope", b"[1, 2]", b"hello"])
async def test_web_rejects_bad_posts_with_a_reason(client, body):
    response = await client.post("/update", data=body)
    assert response.status == 400
    assert (await response.text()).strip()


async def test_web_token_protects_posting():
    monitor = Monitor()
    dashboard = WebDashboard(token="s3cret")
    async with TestClient(TestServer(dashboard.app(monitor))) as client:
        assert (await client.post("/update", data=b"a=1")).status == 401
        wrong = {"Authorization": "Bearer nope"}
        assert (await client.post("/update", data=b"a=1", headers=wrong)).status == 401
        right = {"Authorization": "Bearer s3cret"}
        assert (await client.post("/update", data=b"a=1", headers=right)).status == 204
        assert (await client.get("/")).status == 200  # viewing stays open
    assert monitor["a"].value == "1"


def test_web_start_prints_its_address_and_explains_a_busy_port(capsys):
    first = Monitor().start(outputs=[WebDashboard(port=0)])
    try:
        port = int(capsys.readouterr().err.split(":")[-1].strip(" /\n"))
        assert port > 0
        with pytest.raises(OSError, match=f"port {port} is already in use"):
            Monitor().start(outputs=[WebDashboard(port=port)])
    finally:
        first.stop()
    Monitor().start(outputs=[WebDashboard(port=port, announce=False)]).stop()


async def test_a_program_that_exits_leaves_its_final_values_on_the_page():
    """start()'s dashboard stops with the program: open pages must get the last
    values sent just before exit, and be disconnected promptly."""
    import subprocess
    import sys
    import time

    import aiohttp

    script = (
        "import sys\n"
        "from sightglass import WebDashboard, start\n"
        "monitor = start(outputs=[WebDashboard(port=0)])\n"
        "sys.stdin.readline()  # wait for the test's page to connect\n"
        "for i in range(1, 1001):\n"
        "    monitor.set('n', i)\n"
    )  # ...and exit straight away
    program = await asyncio.create_subprocess_exec(
        sys.executable, "-c", script, stdin=subprocess.PIPE, stderr=subprocess.PIPE
    )
    url = (await program.stderr.readline()).decode().split()[1]
    last = None
    async with aiohttp.ClientSession() as session, session.ws_connect(f"{url}ws") as ws:
        await ws.receive_json(timeout=5)
        program.stdin.write(b"go\n")
        await program.stdin.drain()
        started = time.monotonic()
        async for message in ws:
            last = message.json()["values"].get("n", last)
    await program.communicate()
    assert program.returncode == 0
    assert last == "1000"
    assert time.monotonic() - started < 3  # the page doesn't hold up the exit


def test_terminal_labels_every_row_and_heads_blocks(monkeypatch):
    from sightglass import LogMonitor, Table

    monkeypatch.setattr("shutil.get_terminal_size", lambda: os.terminal_size((80, 40)))
    monitor = Monitor()
    monitor.add(
        TextElement("temperature", label="Temperature", units="°C"),
        Table("error", label="Following error", rows=["X"], columns=["now"]),
        LogMonitor("log", label="Log", lines=1),
    )
    monitor.update({"temperature": 21.5, "error.X.now": 1, "log": "hello"})
    frame = TerminalDisplay().render(monitor).split("\n")
    assert frame[0] == "Temperature  21.5 °C"
    assert frame[1] == "\x1b[1mFollowing error\x1b[0m"
    assert frame[2].startswith("┌")
    assert frame[-2] == "\x1b[1mLog\x1b[0m"
    assert frame[-1].rstrip() == "hello"


async def test_web_returns_current_values_as_json(client):
    client.monitor.update({"speed": 3, "X.velocity": 12})
    response = await client.get("/values")
    assert response.status == 200
    values = await response.json()
    assert values["speed"] == "3"
    assert values["X.velocity"] == {"text": "12.00 mm/s", "ratio": 0.12}


def test_open_browser_opens_the_dashboard_once_it_is_up(monkeypatch):
    opened = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    monkeypatch.setattr("sightglass.web._has_display", lambda: True)
    web = WebDashboard(port=0, announce=False, open_browser=True)
    monitor = Monitor().start(outputs=[web])
    monitor.stop()
    assert opened == [web.url]


@pytest.mark.parametrize(
    ("platform", "env", "expected"),
    [
        ("darwin", {}, True),
        ("win32", {}, True),
        ("linux", {}, False),  # e.g. over SSH: don't start a text browser
        ("linux", {"DISPLAY": ":0"}, True),
        ("linux", {"WAYLAND_DISPLAY": "wayland-0"}, True),
    ],
)
def test_browser_is_only_opened_with_a_display(monkeypatch, platform, env, expected):
    from sightglass.web import _has_display

    monkeypatch.setattr("sys.platform", platform)
    for name in ("DISPLAY", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert _has_display() is expected
