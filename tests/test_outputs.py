import asyncio
import io
import os

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app_monitor import (
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
    assert frame[2].startswith("│ velocity   [") and frame[2].endswith("50.00 mm/s │")
    assert frame[4] == "┌─ job ────────────────────────────────┐"
    assert "25.0%" in frame[5]
    assert all(len(line) == 40 for line in frame[1:7])


def test_terminal_width_follows_a_narrow_terminal(monkeypatch):
    monkeypatch.setattr("shutil.get_terminal_size", lambda: os.terminal_size((30, 40)))
    frame = TerminalDisplay(width=60).render(make_monitor()).split("\n")
    assert len(frame[1]) == 30


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
    assert out.getvalue().endswith("\x1b[?25h\x1b[?1049l")


# -- web ------------------------------------------------------------------------


@pytest.fixture
async def client():
    monitor = make_monitor()
    async with TestClient(TestServer(WebDashboard(fps=200).app(monitor))) as client:
        client.monitor = monitor
        yield client


async def test_web_serves_page_and_assets(client):
    page = await client.get("/")
    assert page.status == 200
    assert "/_app_monitor/app_monitor.js" in await page.text()
    for asset in ["app_monitor.js", "panel.css", "fonts/DSEG14Modern-Regular.ttf"]:
        response = await client.get(f"/_app_monitor/{asset}")
        assert response.status == 200, asset
        assert response.headers["Cache-Control"] == "no-cache"


async def test_web_sends_snapshot_then_only_changes(client):
    ws = await client.ws_connect("/ws")
    snapshot = await ws.receive_json(timeout=1)
    assert snapshot["type"] == "snapshot"
    assert snapshot["values"]["machine"] == {"estop": False, "enabled": False}
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
