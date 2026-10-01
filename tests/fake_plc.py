"""A fake TwinCAT PLC for end-to-end tests: pyads' ADS test server (real TCP,
real AMS packets) with memory, a symbol table and a symbol version, so
AdsSource talks to it exactly as it would to a PLC."""

from __future__ import annotations

import struct
import threading
from dataclasses import dataclass

from pyads import constants
from pyads.testserver import AdsTestServer
from pyads.testserver.handler import AbstractHandler, AmsPacket, AmsResponseData

DATA = 0x4040  # the PLC's variable memory
SYMBOL_NOT_FOUND = 1808
INVALID_GROUP = 1794
INVALID_OFFSET = 1795

ADS_TYPES = {
    "BOOL": constants.ADST_BIT,
    "SINT": constants.ADST_INT8,
    "USINT": constants.ADST_UINT8,
    "INT": constants.ADST_INT16,
    "UINT": constants.ADST_UINT16,
    "DINT": constants.ADST_INT32,
    "UDINT": constants.ADST_UINT32,
    "TIME": constants.ADST_UINT32,
    "LINT": constants.ADST_INT64,
    "REAL": constants.ADST_REAL32,
    "LREAL": constants.ADST_REAL64,
}


@dataclass
class Symbol:
    name: str
    offset: int
    size: int
    data_type: int
    type_name: str
    listed: bool  # in the symbol table, rather than a member found by name

    def entry(self) -> bytes:
        name, type_name = self.name.encode(), self.type_name.encode()
        comment = b""
        strings = name + b"\0" + type_name + b"\0" + comment + b"\0"
        length = 30 + len(strings)
        return (
            struct.pack(
                "<6I3H",
                length,
                DATA,
                self.offset,
                self.size,
                self.data_type,
                8,  # flags: type GUID
                len(name),
                len(type_name),
                len(comment),
            )
            + strings
        )


class FakePlc(AbstractHandler):
    """The PLC's side of ADS. Declare variables with :meth:`add` (and members
    reachable by name with ``listed=False``), change them with :meth:`set`,
    and simulate downloading a new program with :meth:`download`."""

    def __init__(self) -> None:
        self.memory = bytearray(4096)
        self.symbols: dict[str, Symbol] = {}
        self.version = 1
        self.writes: list[tuple[int, bytes]] = []
        self._next = 0
        self._lock = threading.Lock()

    def add(
        self,
        name: str,
        type_name: str,
        data: bytes,
        *,
        data_type: int | None = None,
        offset: int | None = None,
        listed: bool = True,
    ) -> int:
        with self._lock:
            if offset is None:
                offset = self._next
                self._next = -(-(offset + len(data)) // 8) * 8
            if data_type is None:
                data_type = ADS_TYPES.get(type_name, constants.ADST_BIGTYPE)
            self.memory[offset : offset + len(data)] = data
            self.symbols[name.upper()] = Symbol(
                name, offset, len(data), data_type, type_name, listed
            )
            return offset

    def set(self, name: str, data: bytes) -> None:
        with self._lock:
            symbol = self.symbols[name.upper()]
            assert len(data) == symbol.size
            self.memory[symbol.offset : symbol.offset + symbol.size] = data

    def get(self, name: str) -> bytes:
        with self._lock:
            symbol = self.symbols[name.upper()]
            return bytes(self.memory[symbol.offset : symbol.offset + symbol.size])

    def download(self, start: int = 1024) -> None:
        """A new program: no variables (add them again, at new places)."""
        with self._lock:
            self.symbols.clear()
            self.memory[:] = bytes(len(self.memory))
            self._next = start
            self.version = (self.version + 1) % 256

    # -- ADS --

    def handle_request(self, request: AmsPacket) -> AmsResponseData:
        header = request.ams_header
        command = struct.unpack("<H", header.command_id)[0]
        state = struct.pack("<H", struct.unpack("<H", header.state_flags)[0] | 0x0001)
        handler = {
            constants.ADSCOMMAND_READDEVICEINFO: self._device_info,
            constants.ADSCOMMAND_READ: self._read,
            constants.ADSCOMMAND_WRITE: self._write,
            constants.ADSCOMMAND_READWRITE: self._read_write,
            constants.ADSCOMMAND_READSTATE: self._state,
        }.get(command)
        result, payload = 0x701, b""  # service not supported
        if handler is not None:
            with self._lock:
                result, payload = handler(header.data)
        if result and command in (
            constants.ADSCOMMAND_READ,
            constants.ADSCOMMAND_READWRITE,
        ):
            payload = struct.pack("<I", 0)  # replies to reads always have a length
        reply = struct.pack("<I", result) + payload
        return AmsResponseData(state, header.error_code, reply)

    def _device_info(self, data: bytes) -> tuple[int, bytes]:
        return 0, struct.pack("<BBH16s", 3, 1, 4024, b"FakePlc")

    def _state(self, data: bytes) -> tuple[int, bytes]:
        return 0, struct.pack("<HH", constants.ADSSTATE_RUN, 0)

    def _area(self, group: int, offset: int, size: int) -> tuple[int, bytes]:
        if group == constants.ADSIGRP_SYM_VERSION:
            return 0, bytes([self.version])[:size]
        if group == constants.ADSIGRP_SYM_UPLOADINFO2:
            listed = [s for s in self.symbols.values() if s.listed]
            info = struct.pack(
                "<6I", len(listed), sum(len(s.entry()) for s in listed), 0, 0, 0, 0
            )
            return 0, info[:size]
        if group == constants.ADSIGRP_SYM_UPLOAD:
            entries = b"".join(s.entry() for s in self.symbols.values() if s.listed)
            return 0, entries[:size]
        if group != DATA:
            return INVALID_GROUP, b""
        if offset + size > len(self.memory):
            return INVALID_OFFSET, b""
        return 0, bytes(self.memory[offset : offset + size])

    def _read(self, data: bytes) -> tuple[int, bytes]:
        group, offset, size = struct.unpack_from("<III", data)
        error, value = self._area(group, offset, size)
        if error:
            return error, b""
        return 0, struct.pack("<I", len(value)) + value

    def _write(self, data: bytes) -> tuple[int, bytes]:
        group, offset, size = struct.unpack_from("<III", data)
        value = data[12 : 12 + size]
        if group != DATA or offset + size > len(self.memory):
            return INVALID_GROUP if group != DATA else INVALID_OFFSET, b""
        self.memory[offset : offset + size] = value
        self.writes.append((offset, bytes(value)))
        return 0, b""

    def _read_write(self, data: bytes) -> tuple[int, bytes]:
        group, offset, _, write_length = struct.unpack_from("<IIII", data)
        written = data[16 : 16 + write_length]
        if group == constants.ADSIGRP_SYM_INFOBYNAMEEX:
            name = written.split(b"\0", 1)[0].decode()
            symbol = self.symbols.get(name.upper())
            if symbol is None:
                return SYMBOL_NOT_FOUND, b""
            entry = symbol.entry()
            return 0, struct.pack("<I", len(entry)) + entry
        if group == constants.ADSIGRP_SUMUP_READ:
            count = offset
            errors, values = [], []
            for i in range(count):
                area = struct.unpack_from("<III", written, 12 * i)
                error, value = self._area(*area)
                errors.append(error)
                values.append(value if not error else bytes(area[2]))
            reply = struct.pack(f"<{count}I", *errors) + b"".join(values)
            return 0, struct.pack("<I", len(reply)) + reply
        return INVALID_GROUP, b""


class FakePlcServer:
    """Serves a :class:`FakePlc` on 127.0.0.1:48898, where pyads connects."""

    def __init__(self, plc: FakePlc) -> None:
        self.plc = plc
        self._server: AdsTestServer | None = None

    def start(self) -> None:
        self._server = AdsTestServer(handler=self.plc, logging=False)
        self._server.start()

    def stop(self) -> None:
        if self._server is not None:
            # Let its accept loop finish before closing the socket under it.
            self._server._run = False
            self._server.join()
            self._server.close()
            self._server = None
