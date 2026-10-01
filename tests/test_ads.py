"""End to end: AdsSource against a fake PLC, over real ADS (pyads' AMS router
and TCP on 127.0.0.1:48898), through the monitor, HTTP and the CLI."""

import asyncio
import json
import logging
import socket
import struct
import subprocess
import sys
import time
import urllib.request

import pytest

if sys.platform == "win32":
    # pyads uses TwinCAT's own router on Windows, which can't reach a fake PLC.
    pytest.skip(
        "ADS tests need pyads' own router (Linux, macOS)", allow_module_level=True
    )

from aiohttp import ClientSession

from sightglass import Monitor, WebDashboard, WriteError
from sightglass.monitor import WRITES_ENV
from sightglass.sources.ads import AdsSource

from .conftest import run_cli, stop, until
from .fake_plc import FakePlc, FakePlcServer

TARGET = "127.0.0.1.1.1"

AXIS = """
TYPE ST_Axis :
STRUCT
    fPosition : LREAL;
    bEnabled  : BOOL;
    sState    : STRING(10);
END_STRUCT
END_TYPE
TYPE E_Mode : (eIdle, eHoming, eRunning) UINT; END_TYPE
"""


def axis(position: float, enabled: bool, state: str) -> bytes:
    # LREAL at 0, BOOL at 8, STRING(10) at 9..19, padded to 24
    return struct.pack("<d?11s4x", position, enabled, state.encode())


@pytest.fixture
def fake_plc():
    plc = FakePlc()
    plc.server = FakePlcServer(plc)
    plc.server.start()
    yield plc
    plc.server.stop()


@pytest.fixture
def writes_allowed(monkeypatch):
    monkeypatch.setenv(WRITES_ENV, "1")


@pytest.fixture
async def run():
    """Run a monitor with sources (and outputs) in the background for the test."""
    tasks = []

    def start(monitor, *sources, outputs=()):
        tasks.append(asyncio.create_task(monitor.run(sources=sources, outputs=outputs)))

    yield start
    for task in tasks:
        task.cancel()
    for result in await asyncio.gather(*tasks, return_exceptions=True):
        if not isinstance(result, (asyncio.CancelledError, type(None))):
            raise result


def add_program(plc: FakePlc) -> None:
    plc.add("MAIN.nCount", "INT", struct.pack("<h", 5))
    plc.add("MAIN.bRun", "BOOL", b"\x01")
    plc.add("MAIN.eMode", "E_Mode", struct.pack("<H", 2), data_type=18)
    plc.add("MAIN.eOther", "E_Other", struct.pack("<h", 3), data_type=2)
    plc.add(
        "MAIN.aTemps",
        "ARRAY [1..3] OF REAL",
        struct.pack("<3f", 1.5, 2.5, 3.5),
        data_type=4,
    )
    plc.add("MAIN.fbTimer", "TON", bytes(32))
    plc.add("GVL.sName", "STRING(10)", b"hello".ljust(11, b"\0"))
    plc.add("GVL.nSetpoint", "INT", struct.pack("<h", 50))
    offset = plc.add("MAIN.stAxis", "ST_Axis", axis(12.5, True, "idle"))
    plc.add(
        "MAIN.stAxis.fPosition",
        "LREAL",
        struct.pack("<d", 12.5),
        offset=offset,
        listed=False,
    )
    plc.add(
        "MAIN.stAxis.sState",
        "STRING(10)",
        b"idle".ljust(11, b"\0"),
        offset=offset + 9,
        listed=False,
    )


def source(variables, **options) -> AdsSource:
    options.setdefault("interval", 0.02)
    options.setdefault("reconnect_delay", 0.05)
    return AdsSource(TARGET, variables, types=AXIS, **options)


async def test_reads_variables_structs_enums_and_arrays_live(fake_plc, consume):
    add_program(fake_plc)
    monitor = Monitor()
    consume(monitor, source(["MAIN.*", "GVL.sName"]))
    await until(lambda: "GVL.sName" in monitor and "MAIN.stAxis.sState" in monitor)
    assert monitor.snapshot() == {
        "GVL.sName": "hello",
        "MAIN.nCount": "5",
        "MAIN.bRun": True,  # a lamp
        "MAIN.eMode": "eRunning",  # declared: by name
        "MAIN.eOther": "3",  # not declared: its number
        "MAIN.aTemps[1]": "1.5",
        "MAIN.aTemps[2]": "2.5",
        "MAIN.aTemps[3]": "3.5",
        "MAIN.stAxis.fPosition": "12.5",
        "MAIN.stAxis.bEnabled": True,
        "MAIN.stAxis.sState": "idle",
    }
    fake_plc.set("MAIN.nCount", struct.pack("<h", -7))
    await until(lambda: monitor["MAIN.nCount"].value == -7)


async def test_explains_what_it_left_out(fake_plc, consume, caplog):
    add_program(fake_plc)
    fake_plc.add("GVL.stShort", "ST_Axis", bytes(16))  # not what ST_Axis says
    monitor = Monitor()
    with caplog.at_level(logging.WARNING, logger="sightglass"):
        consume(monitor, source(["MAIN.*", "GVL.stShort", "GVL.nope", "IO.*"]))
        await until(lambda: "MAIN.nCount" in monitor)
    text = caplog.text
    assert "the PLC has no variable GVL.nope" in text
    assert "no PLC variables match IO.* (it has GVL.*, MAIN.*)" in text
    assert "MAIN.fbTimer (TON)" in text
    assert "GVL.stShort is 16 bytes on the PLC, but ST_Axis is 24 bytes here" in text
    assert not any(
        id.startswith(("GVL.stShort", "MAIN.fbTimer")) for id in monitor.snapshot()
    )


async def test_follows_a_new_program(fake_plc, consume, caplog):
    """After a download the variables move; reading the old places would show
    the wrong values."""
    fake_plc.add("MAIN.nCount", "INT", struct.pack("<h", 1))
    monitor = Monitor()
    consume(monitor, source(["MAIN.*"]))
    await until(lambda: monitor.snapshot().get("MAIN.nCount") == "1")
    fake_plc.download()
    fake_plc.add("MAIN.bNew", "BOOL", b"\x01")
    fake_plc.add("MAIN.nCount", "INT", struct.pack("<h", 2))
    with caplog.at_level(logging.INFO, logger="sightglass"):
        await until(lambda: monitor.snapshot().get("MAIN.nCount") == "2")
    assert monitor.snapshot()["MAIN.bNew"] is True
    assert "changed; reloading" in caplog.text


async def test_waits_for_the_plc_and_reconnects(fake_plc, consume, caplog):
    add_program(fake_plc)
    fake_plc.server.stop()
    monitor = Monitor()
    with caplog.at_level(logging.WARNING, logger="sightglass"):
        consume(monitor, source(["MAIN.nCount"]))
        await until(lambda: "Waiting for PLC" in caplog.text)
    assert "refused the connection" in caplog.text
    fake_plc.server.start()
    await until(lambda: "MAIN.nCount" in monitor)
    fake_plc.server.stop()  # the PLC goes away mid-session
    await until(lambda: "Lost PLC" in caplog.text, within=5)
    assert "stopped answering" in caplog.text
    fake_plc.set("MAIN.nCount", struct.pack("<h", 9))
    fake_plc.server.start()
    await until(lambda: monitor["MAIN.nCount"].value == 9, within=5)


async def test_without_reconnecting_it_raises_a_clear_error():
    with pytest.raises(ConnectionError, match=r"127\.0\.0\.1 refused the connection"):
        await asyncio.wait_for(
            Monitor().consume(source(["MAIN.x"], reconnect_delay=None)), 5
        )


def test_bad_targets_and_types_are_refused_up_front():
    with pytest.raises(ValueError, match="AMS net id or address"):
        AdsSource("5.1.2.3.1.1:port", ["MAIN.x"])
    with pytest.raises(ValueError, match=r"MAIN\.st: unknown type 'ST_Nope'"):
        AdsSource(TARGET, {"MAIN.st": "ST_Nope"})


# -- writing --------------------------------------------------------------------


def test_writes_need_the_environment_variable():
    with pytest.raises(PermissionError, match=f"set {WRITES_ENV}=1"):
        source({"GVL.nSetpoint": {"write": True}}, allow_writes=True)


async def connected(fake_plc, run, plc_source) -> Monitor:
    add_program(fake_plc)
    monitor = Monitor()
    run(monitor, plc_source)
    await until(lambda: "GVL.nSetpoint" in monitor)
    return monitor


WRITABLE = {
    "GVL.nSetpoint": {"write": True},
    "MAIN.stAxis": {"type": "ST_Axis", "write": True},
    "MAIN.nCount": {},
}


async def test_writes_values_and_shows_what_the_plc_reads_back(
    fake_plc, run, writes_allowed
):
    monitor = await connected(fake_plc, run, source(WRITABLE, allow_writes=True))
    await monitor.submit({"GVL.nSetpoint": "75", "MAIN.stAxis.sState": "moving"})
    assert fake_plc.get("GVL.nSetpoint") == struct.pack("<h", 75)
    assert fake_plc.get("MAIN.stAxis.sState") == b"moving".ljust(11, b"\0")
    await until(lambda: monitor["GVL.nSetpoint"].value == 75)
    await until(lambda: monitor["MAIN.stAxis.sState"].value == "moving")

    monitor.set("GVL.nSetpoint", 76)  # fire and forget, from any thread
    await until(lambda: fake_plc.get("GVL.nSetpoint") == struct.pack("<h", 76))


@pytest.mark.parametrize(
    ("update", "error", "message"),
    [
        ({"MAIN.nCount": 1}, PermissionError, "read from the PLC, not written"),
        ({"GVL.nSetpoint": 99999}, ValueError, "99999 doesn't fit INT: out of range"),
        ({"MAIN.stAxis": 1}, LookupError, "write one of its members"),
        # all or nothing: the valid one isn't written either
        ({"GVL.nSetpoint": 1, "MAIN.stAxis.sState": "x" * 11}, ValueError, "longer"),
    ],
)
async def test_refused_writes_write_nothing(
    fake_plc, run, writes_allowed, update, error, message
):
    monitor = await connected(fake_plc, run, source(WRITABLE, allow_writes=True))
    with pytest.raises(WriteError) as raised:
        await monitor.submit(update)
    assert any(
        isinstance(reason, error) and message in str(reason)
        for reason in raised.value.failures.values()
    ), raised.value
    assert fake_plc.writes == []
    assert "x" not in monitor.snapshot().get("MAIN.stAxis.sState", "")


async def test_writes_are_off_unless_allowed(fake_plc, run, writes_allowed):
    monitor = await connected(fake_plc, run, source(WRITABLE))
    with pytest.raises(WriteError, match="start with --allow-writes"):
        await monitor.submit({"GVL.nSetpoint": 1})
    assert fake_plc.writes == []


async def test_the_environment_variable_guards_every_write(
    fake_plc, run, monkeypatch, writes_allowed
):
    monitor = await connected(fake_plc, run, source(WRITABLE, allow_writes=True))
    monkeypatch.delenv(WRITES_ENV)
    with pytest.raises(WriteError, match=f"set {WRITES_ENV}=1"):
        await monitor.submit({"GVL.nSetpoint": 1})
    assert fake_plc.writes == []


async def test_a_wrong_declaration_never_writes_over_something_else(
    fake_plc, run, writes_allowed
):
    add_program(fake_plc)
    offset = fake_plc.add("GVL.stPair", "ST_Pair", struct.pack("<ii", 1, 2))
    fake_plc.add(
        "GVL.stPair.b", "REAL", struct.pack("<f", 2), offset=offset + 4, listed=False
    )
    plc_source = AdsSource(
        TARGET,
        {"GVL.stPair": {"type": "ST_Pair", "write": True}},
        types="TYPE ST_Pair : STRUCT a : DINT; b : DINT; END_STRUCT END_TYPE",
        allow_writes=True,
        interval=0.02,
    )
    monitor = Monitor()
    run(monitor, plc_source)
    await until(lambda: "GVL.stPair.b" in monitor)
    with pytest.raises(WriteError, match=r"the PLC says GVL\.stPair\.b is a REAL"):
        await monitor.submit({"GVL.stPair.b": 3})
    assert fake_plc.writes == []


async def test_writes_fail_fast_while_disconnected(fake_plc, run, writes_allowed):
    plc_source = source(WRITABLE, allow_writes=True)
    monitor = await connected(fake_plc, run, plc_source)
    fake_plc.server.stop()
    await until(lambda: not plc_source._connected, within=5)
    with pytest.raises(WriteError, match="not connected to the PLC"):
        await monitor.submit({"GVL.nSetpoint": 1})


async def test_writes_over_http_need_the_token(fake_plc, run, writes_allowed):
    add_program(fake_plc)
    monitor = Monitor()
    web = WebDashboard(port=0, announce=False, token="secret")
    plc_source = source(WRITABLE, allow_writes=True)
    run(monitor, plc_source, outputs=[web])
    await until(lambda: web.port != 0 and "GVL.nSetpoint" in monitor)
    auth = {"Authorization": "Bearer secret"}
    url = f"{web.url}update"
    async with ClientSession() as http:
        async with http.post(url, data="GVL.nSetpoint=80", headers=auth) as reply:
            assert reply.status == 204
        assert fake_plc.get("GVL.nSetpoint") == struct.pack("<h", 80)
        async with http.post(url, data="GVL.nSetpoint=x", headers=auth) as reply:
            assert reply.status == 400
            assert "doesn't fit INT" in await reply.text()
        async with http.post(url, data="MAIN.nCount=1", headers=auth) as reply:
            assert reply.status == 403
        fake_plc.server.stop()
        await until(lambda: not plc_source._connected, within=5)
        async with http.post(url, data="GVL.nSetpoint=81", headers=auth) as reply:
            assert reply.status == 409  # 4xx: Client must not retry it later


INTERFACE = f'''
target = "{TARGET}"
interval = 0.05
types = """{AXIS}"""

[variables]
"MAIN.stAxis" = "ST_Axis"
"GVL.nSetpoint" = {{ write = true }}
'''


def test_cli_reads_an_interface_file(fake_plc, tmp_path):
    add_program(fake_plc)
    path = tmp_path / "plc.toml"
    path.write_text(INTERFACE)
    process, url = run_cli("--ads", str(path))
    try:
        deadline = time.monotonic() + 10
        values: dict = {}
        while "MAIN.stAxis.sState" not in values:
            assert time.monotonic() < deadline, values
            time.sleep(0.05)
            with urllib.request.urlopen(f"{url}values") as reply:
                values = json.load(reply)
        assert values == {
            "MAIN.stAxis.fPosition": "12.5",
            "MAIN.stAxis.bEnabled": True,
            "MAIN.stAxis.sState": "idle",
            "GVL.nSetpoint": "50",
        }
    finally:
        _, err = stop(process)
    assert "Traceback" not in err


def test_fake_plc_drops_a_client_that_resets_its_connection(fake_plc):
    """A client stopped mid-conversation can reset its connection rather than
    close it (the CLI, stopped at the end of a test). The fake PLC drops it,
    as a PLC would, instead of failing the test with ConnectionResetError."""
    client = socket.create_connection(("127.0.0.1", 48898))
    client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    time.sleep(0.2)  # accepted
    client.close()  # with no linger: a reset
    deadline = time.monotonic() + 5
    while any(thread.is_alive() for thread in fake_plc.server.connections):
        assert time.monotonic() < deadline, "the connection was never dropped"
        time.sleep(0.01)


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["--ads", "missing.toml"], "can't read missing.toml: No such file"),
        (["--ads", "5.1.2.3.1.1:x", "--vars", "a"], "AMS net id or address"),
    ],
)
def test_cli_explains_bad_plc_arguments(arguments, message, capsys):
    from sightglass.cli import main

    assert main([*arguments, "--port", "0"]) == 1
    assert message in capsys.readouterr().err


@pytest.mark.parametrize(
    ("arguments", "environment", "message"),
    [
        (["--allow-writes"], {WRITES_ENV: "1"}, "--allow-writes needs --token"),
        (["--allow-writes", "--token", "t"], {}, f"set {WRITES_ENV}=1"),
        (["--allow-writes", "--token", "t"], {WRITES_ENV: "0"}, f"set {WRITES_ENV}=1"),
        (["--vars", "MAIN.*"], {}, "--vars goes with a PLC address"),
    ],
)
def test_cli_explains_interface_file_mistakes(
    tmp_path, monkeypatch, arguments, environment, message
):
    path = tmp_path / "plc.toml"
    path.write_text(INTERFACE)
    monkeypatch.delenv(WRITES_ENV, raising=False)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    result = subprocess.run(
        [sys.executable, "-m", "sightglass", "--ads", str(path), *arguments],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1
    assert message in result.stderr
    assert "Traceback" not in result.stderr
