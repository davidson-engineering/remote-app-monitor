import asyncio
import contextlib
import os
import subprocess
import sys

import pytest

from sightglass import WriteError


class FakeSerialDevice:
    """A pseudo-terminal pair: the monitor opens ``port``, the test writes to
    the other end as if it were the device."""

    def __init__(self) -> None:
        import tty

        self._controller, self._device = os.openpty()
        tty.setraw(self._controller)
        tty.setraw(self._device)
        self.port = os.ttyname(self._device)

    def write(self, data: bytes) -> None:
        os.write(self._controller, data)

    def close(self) -> None:
        for fd in (self._controller, self._device):
            with contextlib.suppress(OSError):  # already closed
                os.close(fd)


@pytest.fixture
def fake_device():
    if sys.platform == "win32":
        pytest.skip("pseudo-terminals are POSIX only")
    device = FakeSerialDevice()
    yield device
    device.close()


async def until(condition, within: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + within
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


@pytest.fixture
async def consume():
    """Run ``monitor.consume(source)`` in the background for the test.

    If a source failed, its exception is raised at teardown so the test report
    shows the cause, not just a timeout.
    """
    tasks = []

    def start(monitor, source):
        tasks.append(asyncio.create_task(monitor.consume(source)))

    yield start
    for task in tasks:
        task.cancel()
    for result in await asyncio.gather(*tasks, return_exceptions=True):
        if not isinstance(result, (asyncio.CancelledError, type(None))):
            raise result


def run_cli(
    *argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, cwd=None, env=None
):
    """Start the command; return it and the dashboard URL it prints."""
    process = subprocess.Popen(
        [sys.executable, "-m", "sightglass", "--port", "0", *argv],
        stdin=stdin,
        stdout=stdout,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",  # output cut short by stopping it can end mid-character
        cwd=cwd,
        env={**os.environ, "PYTHONUNBUFFERED": "1", **(env or {})},
    )
    line = process.stderr.readline()
    assert line.startswith("Dashboard: http://"), line
    return process, line.split()[1]


def stop(process):
    process.terminate()
    if process.stdin is not None and process.stdin.closed:
        process.stdin = None  # Python < 3.13's communicate() chokes on it
    return process.communicate(timeout=10)


class Device:
    """A source that reads and writes ids starting with "dev."."""

    def __init__(self, failure: Exception | None = None) -> None:
        self.written: list[dict] = []
        self.failure = failure

    async def updates(self):
        await asyncio.Event().wait()  # reads nothing in these tests
        yield []

    def claims(self, id: str) -> bool:
        return id.startswith("dev.")

    async def write(self, update) -> None:
        if self.failure is not None:
            raise WriteError(dict.fromkeys(update, self.failure))
        self.written.append(dict(update))
