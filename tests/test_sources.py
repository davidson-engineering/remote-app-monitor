"""End-to-end tests: real serial ports (pseudo-terminals) and ZeroMQ sockets."""

import asyncio
import logging
import os
import sys
import threading
import time

import pytest

from app_monitor import CsvDecoder, Monitor, SerialSource, TextElement, ZmqSource
from app_monitor.sources.serialport import find_serial_port

from .conftest import FakeSerialDevice


class Recorder(TextElement):
    """Remembers every value it is given."""

    def __init__(self, id: str):
        super().__init__(id)
        self.history: list[str] = []

    def update(self, value):
        self.history.append(value)
        super().update(value)


async def until(condition, within: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + within
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


@pytest.fixture
async def consume():
    """Run ``monitor.consume(source)`` in the background for the test."""
    tasks = []

    def start(monitor, source):
        tasks.append(asyncio.create_task(monitor.consume(source)))

    yield start
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def test_serial_keeps_up_with_a_fast_device(fake_device, consume):
    """The old reader took one line per poll, so lag grew without bound."""
    monitor = Monitor()
    monitor.add(recorder := Recorder("seq"))
    consume(monitor, SerialSource(fake_device.port, decoder=CsvDecoder(["seq"])))
    await asyncio.sleep(0.2)  # let the reader open the port

    sent = 400

    def device():
        for i in range(sent):  # ~400 Hz
            fake_device.write(f"{i}\n".encode())
            time.sleep(0.0025)

    writer = threading.Thread(target=device)
    writer.start()
    await asyncio.to_thread(writer.join)
    finished = time.monotonic()
    await until(lambda: recorder.value == str(sent - 1))
    assert time.monotonic() - finished < 0.3
    assert recorder.history == [str(i) for i in range(sent)]  # nothing lost


async def test_serial_partial_line_does_not_block_the_event_loop(fake_device, consume):
    monitor = Monitor()
    monitor.add(TextElement("v"))
    consume(monitor, SerialSource(fake_device.port, decoder=CsvDecoder(["v"])))
    await asyncio.sleep(0.2)

    fake_device.write(b"12.")  # the rest of the line is late
    gaps, last = [], time.monotonic()
    for _ in range(20):
        await asyncio.sleep(0.02)
        now = time.monotonic()
        gaps.append(now - last)
        last = now
    assert max(gaps) < 0.15

    fake_device.write(b"5\n")
    await until(lambda: monitor["v"].value == "12.5")


@pytest.mark.skipif(sys.platform == "win32", reason="pseudo-terminals are POSIX only")
async def test_serial_waits_for_device_and_reconnects(tmp_path, consume, caplog):
    link = tmp_path / "ttyFAKE"
    monitor = Monitor()
    monitor.add(TextElement("v"))
    source = SerialSource(str(link), decoder=CsvDecoder(["v"]), reconnect_delay=0.05)
    with caplog.at_level(logging.WARNING, logger="app_monitor"):
        consume(monitor, source)
        await until(lambda: "Waiting for serial device" in caplog.text)

    async def plug_in_and_send(value: str) -> FakeSerialDevice:
        device = FakeSerialDevice()
        temporary = tmp_path / "ttyFAKE.new"
        os.symlink(device.port, temporary)
        os.replace(temporary, link)
        deadline = time.monotonic() + 3
        while monitor["v"].value != value:
            assert time.monotonic() < deadline, f"never received {value!r}"
            device.write(f"{value}\n".encode())
            await asyncio.sleep(0.05)
        return device

    first = await plug_in_and_send("1")
    first.close()  # unplug
    second = await plug_in_and_send("2")
    second.close()


async def test_serial_without_reconnect_raises(tmp_path):
    source = SerialSource(
        str(tmp_path / "missing"), decoder=CsvDecoder(["v"]), reconnect_delay=None
    )
    monitor = Monitor()
    with pytest.raises(Exception, match="missing"):
        await asyncio.wait_for(monitor.consume(source), 2)


def test_find_serial_port_explains_when_nothing_is_found(monkeypatch):
    monkeypatch.setattr("serial.tools.list_ports.comports", lambda: [])
    with pytest.raises(LookupError, match="no USB serial device"):
        find_serial_port()


async def test_zmq_source_end_to_end(consume):
    import zmq

    publisher = zmq.Context.instance().socket(zmq.PUB)
    publisher.setsockopt(zmq.LINGER, 0)
    port = publisher.bind_to_random_port("tcp://127.0.0.1")
    try:
        monitor = Monitor()
        monitor.add_group("X", [TextElement("velocity")])
        monitor.add(TextElement("log"))
        consume(monitor, ZmqSource(f"tcp://127.0.0.1:{port}"))

        deadline = time.monotonic() + 3
        while monitor["log"].value != "motor 2 stalled":  # PUB/SUB joins lazily
            assert time.monotonic() < deadline
            publisher.send_string("X.velocity 12.5")
            publisher.send_string("log motor 2 stalled")
            await asyncio.sleep(0.02)
        assert monitor["X.velocity"].value == "12.5"
    finally:
        publisher.close()
