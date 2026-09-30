"""Decoders turn raw bytes from a source into updates (``{element_id: value}``).

Stream sources (serial) call :meth:`Decoder.feed` with whatever bytes arrived;
the decoder buffers partial messages between calls, and :meth:`Decoder.reset`
on (re)connecting. Message sources (ZeroMQ) call :meth:`Decoder.decode` with
one complete message.

To support a new wire format, subclass :class:`LineDecoder` (one message per
text line) or :class:`Decoder` (anything else).
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import Any

logger = logging.getLogger(__name__)


class DecodeError(ValueError):
    """Raised by a decoder for a message it cannot interpret."""


class Decoder(ABC):
    def __init__(self) -> None:
        self.rejected = 0
        self._joined_mid_stream = False

    @abstractmethod
    def feed(self, data: bytes) -> list[dict[str, Any]]:
        """Consume the next chunk of a byte stream.

        Returns the updates completed by this chunk; incomplete trailing data is
        kept for the next call.
        """

    def decode(self, message: bytes) -> list[dict[str, Any]]:
        """Decode one complete message (not part of a stream)."""
        self.clear()
        try:
            return self.feed(message)
        finally:
            self.clear()

    def reset(self) -> None:
        """Start over, e.g. after (re)connecting: discard buffered partial data.

        A stream is usually joined mid-message, so the first message rejected
        after a reset is expected and only logged at DEBUG.
        """
        self.clear()
        self._joined_mid_stream = True

    def clear(self) -> None:  # noqa: B027 - optional hook for decoders that buffer
        """Discard buffered partial data."""

    def _reject(self, reason: str, data: bytes | str) -> None:
        if self._joined_mid_stream:
            self._joined_mid_stream = False
            logger.debug(
                "%s skipped a partial first message: %.60r", type(self).__name__, data
            )
            return
        # Log the 1st, 10th, 100th, ... problem so a noisy link can't flood the log.
        self.rejected += 1
        if str(self.rejected).rstrip("0") == "1":
            logger.warning(
                "%s rejected %d message(s) so far; latest: %s (%.60r)",
                type(self).__name__,
                self.rejected,
                reason,
                data,
            )
        else:
            logger.debug("%s rejected %.60r: %s", type(self).__name__, data, reason)


class LineDecoder(Decoder):
    """Base for text protocols with one message per line."""

    max_line_length = 4096

    def __init__(self, encoding: str = "utf-8") -> None:
        super().__init__()
        self.encoding = encoding
        self._buffer = bytearray()

    @abstractmethod
    def decode_line(self, line: str) -> dict[str, Any]:
        """Decode one line (without its line ending). Raise DecodeError if invalid."""

    def feed(self, data: bytes) -> list[dict[str, Any]]:
        self._buffer += data
        *lines, rest = self._buffer.split(b"\n")
        if len(rest) > self.max_line_length:
            self._reject("line too long", bytes(rest[:60]))
            rest = bytearray()
        self._buffer = rest
        return self._decode_lines(lines)

    def decode(self, message: bytes) -> list[dict[str, Any]]:
        return self._decode_lines(message.split(b"\n"))

    def clear(self) -> None:
        self._buffer.clear()

    def _decode_lines(
        self, lines: list[bytes] | list[bytearray]
    ) -> list[dict[str, Any]]:
        updates = []
        for raw in lines:
            line = raw.decode(self.encoding, errors="replace").strip()
            if not line:
                continue
            try:
                update = self.decode_line(line)
            except DecodeError as error:
                self._reject(str(error), line)
                continue
            self._joined_mid_stream = False
            if update:
                updates.append(update)
        return updates


class CsvDecoder(LineDecoder):
    """Comma-separated values in a fixed order: ``"1.5,-2.0,OK"``.

    Lines with the wrong number of fields (typically a partial first line after
    connecting) are rejected. Empty fields leave their element unchanged.
    """

    def __init__(
        self, keys: Sequence[str], separator: str = ",", encoding: str = "utf-8"
    ) -> None:
        super().__init__(encoding)
        self.keys = list(keys)
        self.separator = separator

    def decode_line(self, line: str) -> dict[str, Any]:
        fields = line.split(self.separator)
        if len(fields) != len(self.keys):
            raise DecodeError(f"expected {len(self.keys)} fields, got {len(fields)}")
        return {
            key: value.strip()
            for key, value in zip(self.keys, fields, strict=True)
            if value.strip()
        }


class KeyValueDecoder(LineDecoder):
    """One ``"<id> <value>"`` or ``"<id>=<value>"`` pair per line.

    The value is the rest of the line after the first space (or ``=``), so it
    may itself contain spaces: ``"log Motor 2 stalled"``.
    """

    def decode_line(self, line: str) -> dict[str, Any]:
        key, value = _split_pair(line)
        if not key:
            raise DecodeError("expected '<id> <value>'")
        return {key: value}


def _split_pair(line: str) -> tuple[str, str]:
    parts = line.split(maxsplit=1)
    if len(parts) == 2:
        return parts[0], parts[1]
    key, sep, value = line.partition("=")
    return (key.strip(), value.strip()) if sep else ("", "")


class JsonDecoder(LineDecoder):
    """One JSON object per line. Nested objects address groups and fields:
    ``{"X": {"velocity": 1.5}}`` updates ``"X.velocity"``."""

    def decode_line(self, line: str) -> dict[str, Any]:
        try:
            data = json.loads(line)
        except json.JSONDecodeError as error:
            raise DecodeError(f"invalid JSON: {error}") from None
        if not isinstance(data, dict):
            raise DecodeError("expected a JSON object")
        return _flatten(data)


def _flatten(data: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, value in data.items():
        path = f"{prefix}{key}"
        if isinstance(value, Mapping) and value:
            flat.update(_flatten(value, f"{path}."))
        else:
            flat[path] = value
    return flat


class BinaryFrameDecoder(Decoder):
    """The compact binary protocol used by microcontroller firmware::

        0xAA | id | type | value | n | param_1 ... param_n | 0xFF

    ``type`` is ``0x01`` for a little-endian int32 fixed-point number (divided
    by ``scale``) or ``0x02`` for a NUL-terminated UTF-8 string. ``id`` and the
    params are single bytes, translated to names through ``names`` (unknown
    bytes become their decimal value). The update key is the id's name followed
    by the params' names, joined with ``"."``, so params can address a field:
    id ``status`` with params ``(motor1, speed)`` updates ``"status.motor1.speed"``.

    Corrupt frames are skipped and decoding resumes at the next start byte.
    """

    START = 0xAA
    END = 0xFF
    FIXED_POINT = 0x01
    STRING = 0x02
    MAX_STRING = 255

    def __init__(
        self, names: Mapping[int, str] | None = None, scale: float = 1000
    ) -> None:
        super().__init__()
        self.names = dict(names or {})
        self.scale = scale
        self._buffer = bytearray()

    def clear(self) -> None:
        self._buffer.clear()

    def feed(self, data: bytes) -> list[dict[str, Any]]:
        buffer = self._buffer
        buffer += data
        updates = []
        while buffer:
            start = buffer.find(self.START)
            if start < 0:
                self._reject("bytes outside a frame", bytes(buffer))
                buffer.clear()
                break
            if start > 0:
                self._reject("bytes outside a frame", bytes(buffer[:start]))
                del buffer[:start]
            try:
                frame = self._parse(buffer)
            except DecodeError as error:
                self._reject(str(error), bytes(buffer[:16]))
                del buffer[:1]  # resynchronise on the next start byte
                continue
            if frame is None:
                break  # incomplete; wait for more data
            update, length = frame
            self._joined_mid_stream = False
            updates.append(update)
            del buffer[:length]
        return updates

    def _parse(self, buf: bytearray) -> tuple[dict[str, Any], int] | None:
        """Parse the frame at the start of ``buf``: (update, frame length), or
        None if ``buf`` doesn't hold the whole frame yet."""
        if len(buf) < 3:
            return None
        element, kind, i = buf[1], buf[2], 3
        value: float | str
        if kind == self.FIXED_POINT:
            if len(buf) < i + 4:
                return None
            value = int.from_bytes(buf[i : i + 4], "little", signed=True) / self.scale
            i += 4
        elif kind == self.STRING:
            end = buf.find(0, i, i + self.MAX_STRING + 1)
            if end < 0:
                if len(buf) - i > self.MAX_STRING:
                    raise DecodeError("unterminated string")
                return None
            value = buf[i:end].decode("utf-8", errors="replace")
            i = end + 1
        else:
            raise DecodeError(f"unknown value type 0x{kind:02X}")
        if len(buf) < i + 1:
            return None
        count = buf[i]
        i += 1
        if len(buf) < i + count + 1:
            return None
        params = buf[i : i + count]
        i += count
        if buf[i] != self.END:
            raise DecodeError(f"expected end byte 0x{self.END:02X}, got 0x{buf[i]:02X}")
        key = ".".join(self._name(b) for b in (element, *params))
        return {key: value}, i + 1

    def _name(self, byte: int) -> str:
        return self.names.get(byte, str(byte))
