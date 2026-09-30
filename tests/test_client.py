"""The HTTP client against a real dashboard."""

import socket
import subprocess
import sys
import time

import pytest

from app_monitor import LogMonitor, Monitor, WebDashboard
from app_monitor.client import Client, send


@pytest.fixture
def dashboard():
    web = WebDashboard(port=0, announce=False, token=None)
    monitor = Monitor().start(outputs=[web])
    monitor.url = web.url
    yield monitor
    monitor.stop()


def wait_for(condition, within=3.0):
    deadline = time.monotonic() + within
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.01)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_client_sends_in_order(dashboard):
    dashboard.add(LogMonitor("log", lines=100))  # from this (non-loop) thread
    with Client(dashboard.url) as client:
        for i in range(50):
            client.set("log", f"line {i}")
        client.update({"progress": 50, "status": "done"})
        assert client.flush()
    wait_for(lambda: "status" in dashboard)
    assert list(dashboard["log"].entries) == [f"line {i}" for i in range(50)]
    assert dashboard["progress"].value == 50


def test_send_once(dashboard):
    send({"job": {"state": "done"}}, dashboard.url)
    wait_for(lambda: "job.state" in dashboard)
    assert dashboard["job.state"].value == "done"


def test_send_explains_an_unreachable_dashboard():
    with pytest.raises(ConnectionError, match="can't send to the dashboard at"):
        send({"a": 1}, f"http://127.0.0.1:{free_port()}", timeout=1)


def test_client_keeps_updates_until_the_dashboard_starts():
    port = free_port()
    client = Client(f"http://127.0.0.1:{port}", interval=0.01)
    client.set("status", "queued before the dashboard existed")
    assert not client.flush(timeout=0.3)  # nothing listening yet
    monitor = Monitor().start(outputs=[WebDashboard(port=port, announce=False)])
    try:
        assert client.flush(timeout=5)
        wait_for(lambda: "status" in monitor)
        assert monitor["status"].value == "queued before the dashboard existed"
    finally:
        client.close(timeout=0)
        monitor.stop()


def test_client_drops_updates_the_dashboard_rejects(caplog):
    web = WebDashboard(port=0, announce=False, token="right")
    monitor = Monitor().start(outputs=[web])
    try:
        with Client(web.url, token="wrong") as client:
            client.set("a", 1)
            assert client.flush()  # rejected, not retried forever
        assert "rejected 1 update(s): HTTP 401" in caplog.text
        with Client(web.url, token="right") as client:
            client.set("a", 2)
            assert client.flush()
        wait_for(lambda: "a" in monitor)
    finally:
        monitor.stop()


def test_short_script_delivers_on_exit(dashboard):
    """The ZeroMQ PUB/SUB route lost these messages entirely."""
    script = (
        "from app_monitor.client import Client\n"
        f"Client({dashboard.url!r}).set('status', 'quick script ran')\n"
    )
    subprocess.run([sys.executable, "-c", script], check=True, timeout=20)
    wait_for(lambda: "status" in dashboard)
    assert dashboard["status"].value == "quick script ran"


def test_client_needs_no_optional_dependencies():
    script = (
        "import sys\n"
        "import app_monitor.client\n"
        "loaded = {'aiohttp', 'zmq', 'serial'} & set(sys.modules)\n"
        "assert not loaded, loaded\n"
    )
    subprocess.run([sys.executable, "-c", script], check=True, timeout=20)
