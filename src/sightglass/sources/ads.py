"""Beckhoff TwinCAT PLCs over ADS (requires ``pyads``)."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import ctypes
import logging
import queue
import socket
import struct
import sys
import threading
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import aclosing
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

try:
    import pyads
except OSError as error:  # pragma: no cover - Windows without TwinCAT
    raise ImportError(
        "pyads needs Beckhoff's ADS router (TcAdsDll.dll) on Windows: install "
        f"TwinCAT 3 (XAE, XAR, or the TC1000 ADS setup) ({error})"
    ) from error
from pyads import constants
from pyads.errorcodes import ERROR_CODES
from pyads.structs import SAdsSumRequest, SAdsSymbolEntry

from .. import plc
from ..monitor import Update, WriteError, writes_guard_message, writes_permitted
from ._thread import Emit, thread_items

logger = logging.getLogger(__name__)

ADS_TCP_PORT = 48898
DEFAULT_AMS_PORT = 851  # the first PLC runtime of TwinCAT 3 (TwinCAT 2: 801)
MAX_SUM_COMMANDS = 500  # Beckhoff's limit per sum command
MAX_PATTERN_VALUES = 100  # larger arrays matched by a pattern are left out

_PORT_NOT_FOUND = 6
_MACHINE_NOT_FOUND = 7
_SYMBOL_NOT_FOUND = 1808
_TIMEOUT = 1861

# Enums and aliases nobody declared are read as their underlying number.
_BY_ADS_TYPE = {
    constants.ADST_INT8: "SINT",
    constants.ADST_UINT8: "USINT",
    constants.ADST_INT16: "INT",
    constants.ADST_UINT16: "UINT",
    constants.ADST_INT32: "DINT",
    constants.ADST_UINT32: "UDINT",
    constants.ADST_INT64: "LINT",
    constants.ADST_UINT64: "ULINT",
    constants.ADST_REAL32: "REAL",
    constants.ADST_REAL64: "LREAL",
    constants.ADST_BIT: "BOOL",
}

_FAILURES = (pyads.ADSError, OSError, RuntimeError)


class AdsSource:
    """Reads variables from a Beckhoff TwinCAT PLC over ADS, and optionally
    writes values sent to the ones marked writable.

    Every ``interval`` seconds, all the variables are read in one request (an
    ADS sum read), and every value is sent to the monitor under the variable's
    name: ``"MAIN.nCount"``, or one id per member and element of structs and
    arrays (``"MAIN.stAxis.fPosition"``, ``"GVL.aTemps[1]"``). When the PLC
    program is downloaded again the variables are looked up afresh.

    ``variables`` lists names, where ``*`` matches any characters
    (``"MAIN.*"``: the top-level variables of ``MAIN``), or maps each name to
    its type (``"ST_Axis"``) or to options, ``{"type": ..., "write": True}``.
    ``types`` declares struct, enum and alias types in Structured Text, as in
    the PLC project; a variable whose type is declared there is split into its
    members, and checked against the size the PLC reports.

    Writing needs three things, so nothing is written by accident: the
    variable is marked ``write``, the source is created with
    ``allow_writes=True``, and the environment variable
    ``SIGHTGLASS_ALLOW_WRITES`` is ``1``. Values sent to a writable variable
    (``monitor.set``, ``POST /update``, ``Client``) are then checked against
    the PLC's own type for it and written; the display shows what is read
    back. Writes are never retried.

    If the PLC can't be reached, or stops answering, it retries every
    ``reconnect_delay`` seconds (set it to None to raise instead).

    Args:
        target: the PLC's AMS net id (``"5.12.34.56.1.1"``) or IP address or
            host name, optionally followed by ``:port`` (default 851, the
            first TwinCAT 3 PLC; TwinCAT 2 uses 801).
        variables: what to read, as above.
        types: Structured Text ``TYPE ... END_TYPE`` declarations.
        ip_address: the PLC's IP address, if it isn't the first four parts of
            its AMS net id.
        interval: seconds between reads.
        allow_writes: write values sent to variables marked ``write``.
        timeout: seconds to wait for the PLC to answer a request.
    """

    def __init__(
        self,
        target: str,
        variables: plc.VariableSpec,
        *,
        types: str = "",
        ip_address: str | None = None,
        interval: float = 0.1,
        allow_writes: bool = False,
        reconnect_delay: float | None = 2.0,
        timeout: float = 2.0,
    ) -> None:
        if interval <= 0:
            raise ValueError("interval must be positive")
        self.target = target
        self.net_id, self.host, self.port = _parse_target(target)
        self.ip_address = ip_address
        self.variables = plc.parse_variables(variables)
        self.types = plc.Types(types)
        self._declared = plc.declared_types(self.variables, self.types)
        self.interval = interval
        self.reconnect_delay = reconnect_delay
        self.timeout = timeout
        self.allow_writes = allow_writes
        if allow_writes and not writes_permitted():
            raise PermissionError(writes_guard_message())
        self._writes: queue.Queue[_Write] = queue.Queue()
        self._connected = False
        self._writable: dict[str, plc.Leaf] = {}  # upper-case id -> leaf
        self._local_address: str | None = None  # this machine's IP towards the PLC
        self._local_net_id: str | None = None

    @classmethod
    def from_file(cls, path: str | Path, **options: Any) -> AdsSource:
        """A source described by an interface file (TOML); ``options`` (e.g.
        ``allow_writes=True``) override what it says."""
        return cls(**{**plc.read_interface(path), **options})

    @property
    def writable(self) -> list[str]:
        """The variables values may be written to (if writes are allowed)."""
        return [variable.name for variable in self.variables if variable.write]

    async def updates(self) -> AsyncIterator[list[Update]]:
        if self.allow_writes and self.writable:
            logger.warning(
                "Writes to PLC %s are allowed for %s",
                self.target,
                ", ".join(self.writable),
            )
        async with aclosing(thread_items(self._run, repr(self))) as items:
            async for values in items:
                yield [values]  # type: ignore[list-item]

    # -- writing (called on the monitor's event loop) -------------------------

    def claims(self, id: str) -> bool:
        return any(variable.covers(id) for variable in self.variables)

    async def write(self, update: Mapping[str, Any]) -> None:
        """Write ``{id: value}`` to the PLC, all or nothing: if any value can't
        be written (not writable, doesn't fit its type), nothing is."""
        failures: dict[str, BaseException] = {}
        jobs: list[_Write] = []
        for id, value in update.items():
            try:
                jobs.append(self._prepare(id, value))
            except (PermissionError, ValueError, LookupError, ConnectionError) as error:
                failures[id] = error
        if failures:
            raise WriteError(failures)
        for job in jobs:
            self._writes.put(job)
        results = await asyncio.gather(
            *(asyncio.wrap_future(job.done) for job in jobs), return_exceptions=True
        )
        failures = {
            job.id: result
            for job, result in zip(jobs, results, strict=True)
            if isinstance(result, BaseException)
        }
        if failures:
            raise WriteError(failures)

    def _prepare(self, id: str, value: Any) -> _Write:
        if not any(
            variable.write and variable.covers(id) for variable in self.variables
        ):
            raise PermissionError(
                "read from the PLC, not written: mark it write = true in the "
                "interface file to write it"
            )
        if not self.allow_writes:
            raise PermissionError(
                "writes to the PLC are off: start with --allow-writes "
                "(allow_writes=True)"
            )
        if not writes_permitted():
            raise PermissionError(writes_guard_message())
        if not self._connected:
            raise ConnectionError(f"not connected to the PLC {self.target}")
        leaf = self._writable.get(id.upper())
        if leaf is None:
            raise LookupError(
                "not a single value on the PLC: write one of its members or elements"
            )
        try:
            data = leaf.type.encode(value)
        except (TypeError, ValueError) as error:
            message = f"{value!r} doesn't fit {leaf.type.name}: {error}"
            raise ValueError(message) from None
        return _Write(id, leaf.path, leaf.type.name, data, value)

    # -- the background thread ------------------------------------------------

    def _run(self, emit: Emit, stop: threading.Event) -> None:
        waiting_logged = False
        while not stop.is_set():
            try:
                connection = self._connect()
            except _FAILURES as error:
                message = self._explain(error, connecting=True)
                if self.reconnect_delay is None:
                    raise ConnectionError(message) from None
                log = logger.debug if waiting_logged else logger.warning
                log("Waiting for PLC %s: %s", self.target, message)
                waiting_logged = True
                self._pause(stop, self.reconnect_delay)
                continue
            waiting_logged = False
            try:
                self._session(connection, emit, stop)
            except _FAILURES as error:
                message = self._explain(error, connecting=False)
                if self.reconnect_delay is None:
                    raise ConnectionError(message) from None
                logger.warning("Lost PLC %s: %s", self.target, message)
            finally:
                self._connected = False
                self._writable = {}
                with contextlib.suppress(Exception):  # it may be gone already
                    connection.close()
                self._fail_writes(ConnectionError(f"lost the PLC {self.target}"))
            if not stop.is_set() and self.reconnect_delay is not None:
                self._pause(stop, self.reconnect_delay)

    def _connect(self) -> pyads.Connection:
        ip = self.ip_address
        if ip is None:
            ip = socket.gethostbyname(self.host) if self.host else _ip_of(self.net_id)
        if sys.platform != "win32":
            # pyads connects directly here, and takes minutes to give up on an
            # address that doesn't answer; find out quickly instead.
            with socket.create_connection((ip, ADS_TCP_PORT), self.timeout) as probe:
                self._local_address = probe.getsockname()[0]
        connection = pyads.Connection(self.net_id, self.port, ip)
        try:
            connection.open()
            connection.set_timeout(round(self.timeout * 1000))
            if (address := connection.get_local_address()) is not None:
                self._local_net_id = address.netid
            connection.read_state()
        except BaseException:
            with contextlib.suppress(Exception):
                connection.close()
            raise
        return connection

    def _session(
        self, connection: pyads.Connection, emit: Emit, stop: threading.Event
    ) -> None:
        plan = self._resolve(connection)
        next_read = time.monotonic()
        while not stop.is_set():
            values, changed = self._read(connection, plan)
            if changed:
                logger.info("The program on PLC %s changed; reloading", self.target)
                plan = self._resolve(connection)
                continue
            if values:
                emit(values)
            next_read = max(next_read + self.interval, time.monotonic())
            self._serve_writes(connection, stop, next_read)

    def _resolve(self, connection: pyads.Connection) -> _Plan:
        """Look up every variable on the PLC and work out how to read it."""
        version = _symbol_version(connection)
        reads: dict[str, _Read] = {}
        problems: list[str] = []
        skipped: list[str] = []
        table: list[_Symbol] | None = None
        exact = [variable for variable in self.variables if not variable.is_pattern]
        patterns = [variable for variable in self.variables if variable.is_pattern]
        for variable in exact:
            try:
                symbol = _symbol(connection, variable.name)
            except pyads.ADSError as error:
                if error.err_code != _SYMBOL_NOT_FOUND:
                    raise
                problems.append(f"the PLC has no variable {variable.name}")
                continue
            try:
                kind = self._declared.get(variable.name.upper()) or self._type_of(
                    symbol
                )
            except plc.UnknownType as error:
                problems.append(
                    f"{variable.name} is a {error.name}: declare its type in types, "
                    "or name the members you want"
                )
                continue
            except plc.PlcTypeError as error:
                problems.append(f"{variable.name} can't be read: {error}")
                continue
            if kind.size != symbol.size:
                problems.append(
                    f"{variable.name} is {symbol.size} bytes on the PLC, but "
                    f"{kind.name} is {kind.size} bytes here: check the declaration "
                    "(member types and order, pack_mode)"
                )
                continue
            reads[variable.name.upper()] = _Read(
                symbol, list(kind.leaves(variable.name)), writable=variable.write
            )
        for variable in patterns:
            if table is None:
                table = _upload(connection)
            matched = [s for s in table if plc.matches(variable.name, s.name)]
            if not matched:
                prefixes = sorted({s.name.split(".")[0] + ".*" for s in table})
                problems.append(
                    f"no PLC variables match {variable.name} (it has "
                    f"{', '.join(prefixes[:12]) or 'none'})"
                )
            for symbol in matched:
                if symbol.name.upper() in reads:
                    continue
                try:
                    kind = self._type_of(symbol)
                    if kind.size != symbol.size:
                        raise plc.PlcTypeError(f"{symbol.size} bytes on the PLC")
                    leaves = list(kind.leaves(symbol.name))
                    if len(leaves) > MAX_PATTERN_VALUES:
                        raise plc.PlcTypeError(f"{len(leaves)} values")
                except plc.PlcTypeError:
                    skipped.append(f"{symbol.name} ({symbol.type_name})")
                    continue
                reads[symbol.name.upper()] = _Read(symbol, leaves, writable=False)
        if skipped:
            shown = ", ".join(skipped[:5]) + (", ..." if len(skipped) > 5 else "")
            problems.append(
                f"left out {len(skipped)} PLC variable(s) matching a pattern that "
                f"aren't plain values or declared types: {shown}; declare their "
                "types in types, or name the members you want"
            )
        for problem in problems:
            logger.warning("PLC %s: %s", self.target, problem)
        plan = _Plan(list(reads.values()), version)
        self._writable = {
            leaf.path.upper(): leaf
            for read in plan.reads
            if read.writable
            for leaf in read.leaves
        }
        self._connected = True
        count = sum(len(read.leaves) for read in plan.reads)
        if not plan.reads:
            logger.warning("PLC %s: nothing to read", self.target)
        logger.info(
            "Reading %d values from %d variables on PLC %s",
            count,
            len(plan.reads),
            self.target,
        )
        return plan

    def _type_of(self, symbol: _Symbol) -> plc.PlcType:
        try:
            return self.types.parse(symbol.type_name)
        except plc.UnknownType:
            number = _BY_ADS_TYPE.get(symbol.data_type)
            if number is None or plc.ELEMENTARY[number].size != symbol.size:
                raise
            return replace(plc.ELEMENTARY[number], name=symbol.type_name)

    def _read(
        self, connection: pyads.Connection, plan: _Plan
    ) -> tuple[dict[str, Any], bool]:
        """Read every variable; return the values, and whether the program changed."""
        requests = [
            (r.symbol.group, r.symbol.offset, r.symbol.size) for r in plan.reads
        ]
        if plan.version is not None:
            requests.append((constants.ADSIGRP_SYM_VERSION, 0, 1))
        if not requests:
            return {}, False
        results = _sum_read(connection, requests)
        if plan.version is not None:
            error, data = results.pop()
            if not error and data[0] != plan.version:
                return {}, True
        values: dict[str, Any] = {}
        for read, (error, data) in zip(plan.reads, results, strict=True):
            if error:
                if not read.failing:
                    logger.warning(
                        "PLC %s: can't read %s: ADS error %d",
                        self.target,
                        read.symbol.name,
                        error,
                    )
                read.failing = True
                continue
            read.failing = False
            for leaf in read.leaves:
                values[leaf.path] = leaf.type.decode(data, leaf.offset)
        return values, False

    def _serve_writes(
        self, connection: pyads.Connection, stop: threading.Event, until: float
    ) -> None:
        """Carry out writes until it's time for the next read."""
        while not stop.is_set():
            remaining = until - time.monotonic()
            if remaining <= 0:
                return
            try:
                job = self._writes.get(timeout=min(remaining, 0.1))
            except queue.Empty:
                continue
            try:
                self._execute(connection, job)
            except pyads.ADSError as error:
                reason = f"the PLC refused it: {_ads_message(error)}"
                job.done.set_exception(RuntimeError(reason))
            except Exception as error:
                job.done.set_exception(error)
            else:
                job.done.set_result(None)

    def _execute(self, connection: pyads.Connection, job: _Write) -> None:
        # Check the PLC's own idea of the variable, so a mistaken declaration
        # can never write over something else.
        symbol = _symbol(connection, job.path)
        expected = plc.canonical_type_name(job.type_name)
        if plc.canonical_type_name(symbol.type_name) != expected or symbol.size != len(
            job.data
        ):
            raise RuntimeError(
                f"the PLC says {job.path} is a {symbol.type_name} ({symbol.size} "
                f"bytes), not a {job.type_name} ({len(job.data)} bytes); nothing "
                "was written"
            )
        connection.write(
            symbol.group, symbol.offset, job.data, ctypes.c_ubyte * len(job.data)
        )
        logger.info("Wrote %r to %s on PLC %s", job.value, job.path, self.target)

    def _pause(self, stop: threading.Event, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not stop.is_set() and time.monotonic() < deadline:
            self._fail_writes(
                ConnectionError(f"not connected to the PLC {self.target}")
            )
            stop.wait(min(0.1, max(0.0, deadline - time.monotonic())))

    def _fail_writes(self, error: Exception) -> None:
        while True:
            try:
                job = self._writes.get_nowait()
            except queue.Empty:
                return
            job.done.set_exception(error)

    def _explain(self, error: BaseException, *, connecting: bool) -> str:
        """One line saying what went wrong, and what to check. A PLC that
        never answers usually lacks a route; one that stops answering went
        away."""
        address = self.ip_address or self.host or _ip_of(self.net_id)
        if isinstance(error, pyads.ADSError):
            code = error.err_code
            if code == _PORT_NOT_FOUND:
                return (
                    f"nothing answers on ADS port {self.port} (is the PLC running? "
                    "TwinCAT 3 PLCs use port 851, TwinCAT 2 801)"
                )
            if code in (_MACHINE_NOT_FOUND, _TIMEOUT) and not connecting:
                return f"it stopped answering ({_ads_message(error)})"
            if code in (_MACHINE_NOT_FOUND, _TIMEOUT) and sys.platform != "win32":
                local = self._local_net_id or (
                    f"{self._local_address}.1.1" if self._local_address else "?"
                )
                return (
                    f"no answer (ADS error {code}): the PLC needs a route to this "
                    f"machine, AMS net id {local} at {self._local_address or '?'}"
                )
            return _ads_message(error)
        if isinstance(error, TimeoutError):
            return f"{address}:{ADS_TCP_PORT} didn't answer within {self.timeout:g} s"
        if isinstance(error, ConnectionRefusedError):
            return f"{address} refused the connection (is TwinCAT running there?)"
        if isinstance(error, socket.gaierror):
            return f"unknown host {self.host}"
        return str(error) or type(error).__name__

    def __repr__(self) -> str:
        return f"AdsSource({self.target!r})"


@dataclass(frozen=True)
class _Symbol:
    name: str
    group: int
    offset: int
    size: int
    data_type: int
    type_name: str


@dataclass
class _Read:
    symbol: _Symbol
    leaves: list[plc.Leaf]
    writable: bool
    failing: bool = False


@dataclass
class _Plan:
    reads: list[_Read]
    version: int | None


@dataclass
class _Write:
    id: str
    path: str
    type_name: str
    data: bytes
    value: Any
    done: concurrent.futures.Future[None] = field(
        default_factory=concurrent.futures.Future
    )


def _ads_message(error: pyads.ADSError) -> str:
    text = ERROR_CODES.get(error.err_code, "unknown error")
    return f"{text} (ADS error {error.err_code})"


def _parse_target(target: str) -> tuple[str | None, str | None, int]:
    """(net id, host, port) from ``"5.12.34.56.1.1:851"`` or ``"plc.local"``."""
    address, colon, port = target.strip().rpartition(":")
    if not colon:
        address, port = target.strip(), str(DEFAULT_AMS_PORT)
    if not port.isdigit() or not address:
        raise ValueError(
            f"expected an AMS net id or address, then :port, not {target!r}"
        )
    parts = address.split(".")
    if len(parts) == 6 and all(part.isdigit() and int(part) < 256 for part in parts):
        return address, None, int(port)
    return None, address, int(port)


def _ip_of(net_id: str | None) -> str:
    return ".".join((net_id or "").split(".")[:4])


def _text(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def _symbol(connection: pyads.Connection, name: str) -> _Symbol:
    entry = connection.read_write(
        constants.ADSIGRP_SYM_INFOBYNAMEEX,
        0,
        SAdsSymbolEntry,
        name,
        constants.PLCTYPE_STRING,
        return_ctypes=True,
        check_length=False,
    )
    return _Symbol(
        entry.name,
        entry.iGroup,
        entry.iOffs,
        entry.size,
        entry.dataType,
        entry.symbol_type,
    )


def _upload(connection: pyads.Connection) -> list[_Symbol]:
    """Every top-level variable in the PLC's symbol table."""
    info = connection.read(
        constants.ADSIGRP_SYM_UPLOADINFO2,
        0,
        ctypes.c_ubyte * 24,
        return_ctypes=True,
        check_length=False,
    )
    count, length = struct.unpack_from("<II", bytes(info))
    if not count:
        return []
    data = bytes(
        connection.read(
            constants.ADSIGRP_SYM_UPLOAD, 0, ctypes.c_ubyte * length, return_ctypes=True
        )
    )
    symbols, start = [], 0
    for _ in range(count):
        entry_length, group, offset, size, data_type, _, name_length, type_length = (
            struct.unpack_from("<6I2H", data, start)
        )
        text = start + 30
        name = _text(data[text : text + name_length])
        text += name_length + 1
        symbols.append(
            _Symbol(
                name,
                group,
                offset,
                size,
                data_type,
                _text(data[text : text + type_length]),
            )
        )
        if entry_length <= 0:
            break
        start += entry_length
    return symbols


def _symbol_version(connection: pyads.Connection) -> int | None:
    """The PLC's symbol version, which changes when a program is downloaded,
    or None if it doesn't have one."""
    try:
        return connection.read(constants.ADSIGRP_SYM_VERSION, 0, constants.PLCTYPE_BYTE)
    except pyads.ADSError:
        return None


def _sum_read(
    connection: pyads.Connection, requests: list[tuple[int, int, int]]
) -> list[tuple[int, bytes]]:
    """Read many (index group, offset, size) areas at once: (error, bytes) each."""
    results: list[tuple[int, bytes]] = []
    for first in range(0, len(requests), MAX_SUM_COMMANDS):
        chunk = requests[first : first + MAX_SUM_COMMANDS]
        commands = (SAdsSumRequest * len(chunk))()
        for command, (group, offset, size) in zip(commands, chunk, strict=True):
            command.iGroup, command.iOffset, command.size = group, offset, size
        data = bytes(
            connection.read_write(
                constants.ADSIGRP_SUMUP_READ,
                len(chunk),
                None,
                commands,
                None,
                return_ctypes=True,
                check_length=False,
            )
        )
        position = 4 * len(chunk)
        for i, (_, _, size) in enumerate(chunk):
            error = struct.unpack_from("<I", data, 4 * i)[0]
            results.append((error, data[position : position + size]))
            position += size
    return results
