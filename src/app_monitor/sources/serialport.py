"""Serial port source (requires ``pyserial``)."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator, Callable
from typing import Any

import serial
from serial.tools import list_ports

from ..decoders import Decoder
from ..monitor import Update

logger = logging.getLogger(__name__)

_CONNECTED = object()


def find_serial_port() -> str:
    """Return the first USB serial device, e.g. an Arduino.

    Raises LookupError (listing what was found) if there is none.
    """
    ports = sorted(list_ports.comports(), key=lambda port: port.device)
    usb = [port.device for port in ports if port.vid is not None]
    if not usb:
        found = ", ".join(port.device for port in ports) or "none"
        raise LookupError(f"no USB serial device found (serial ports: {found})")
    return usb[0]


class SerialSource:
    """Reads a serial port and decodes what arrives.

    Reading happens on a background thread, and every read takes all bytes
    available, so the monitor always shows the latest data no matter how fast
    the device sends and a stalled device can't block the event loop.

    If the port can't be opened or the device disconnects, it retries every
    ``reconnect_delay`` seconds (set it to None to raise instead).

    Args:
        port: device path, or ``"auto"`` for the first USB serial device.
        baudrate: must match the device.
        decoder: how to interpret the bytes, e.g. ``CsvDecoder([...])``.
        **serial_options: passed to :class:`serial.Serial`.
    """

    read_timeout = 0.1

    def __init__(
        self,
        port: str = "auto",
        baudrate: int = 115200,
        *,
        decoder: Decoder,
        reconnect_delay: float | None = 2.0,
        **serial_options: Any,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.decoder = decoder
        self.reconnect_delay = reconnect_delay
        self.serial_options = serial_options

    async def updates(self) -> AsyncIterator[list[Update]]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Any] = asyncio.Queue()
        stop = threading.Event()

        def emit(item: object) -> None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, item)
            except RuntimeError:  # event loop already closed
                stop.set()

        reader = threading.Thread(
            target=self._read_forever, args=(emit, stop), name=repr(self), daemon=True
        )
        reader.start()
        try:
            while True:
                item = await queue.get()
                if item is _CONNECTED:
                    self.decoder.reset()  # drop any partial message from before
                elif isinstance(item, BaseException):
                    raise item
                elif batch := self.decoder.feed(item):
                    yield batch
        finally:
            stop.set()
            await asyncio.to_thread(reader.join)

    def _read_forever(
        self, emit: Callable[[object], None], stop: threading.Event
    ) -> None:
        """Background thread: open, read and reconnect until ``stop`` is set."""
        waiting_logged = False
        while not stop.is_set():
            try:
                connection = self._open()
            except (serial.SerialException, OSError, LookupError) as error:
                if self.reconnect_delay is None:
                    emit(error)
                    return
                log = logger.debug if waiting_logged else logger.warning
                log("Waiting for serial device %s: %s", self.port, error)
                waiting_logged = True
                stop.wait(self.reconnect_delay)
                continue
            waiting_logged = False
            logger.info("Connected to %s at %d baud", connection.port, self.baudrate)
            emit(_CONNECTED)
            try:
                with connection:
                    while not stop.is_set():
                        if data := connection.read(connection.in_waiting or 1):
                            emit(data)
            except (serial.SerialException, OSError) as error:
                if self.reconnect_delay is None:
                    emit(error)
                    return
                logger.warning("Lost serial device %s: %s", connection.port, error)
                stop.wait(self.reconnect_delay)

    def _open(self) -> serial.Serial:
        port = find_serial_port() if self.port == "auto" else self.port
        return serial.Serial(
            port, self.baudrate, timeout=self.read_timeout, **self.serial_options
        )

    def __repr__(self) -> str:
        return f"SerialSource({self.port!r}, {self.baudrate})"
