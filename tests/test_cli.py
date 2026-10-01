"""The sightglass command, run the way a user would."""

import asyncio
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

import aiohttp
import pytest

from sightglass import (
    SerialSource,
    StdinSource,
    TerminalDisplay,
    WebDashboard,
    ZmqSource,
)
from sightglass.cli import build, parser


def args(*argv):
    return parser().parse_args(argv)


class Terminal(io.StringIO):
    """stdin when nothing is piped in."""

    def isatty(self):
        return True


def test_build_serial_csv(monkeypatch):
    monkeypatch.setattr("sys.stdin", Terminal())
    _, sources, outputs = build(
        args("--serial", "auto", "--csv", "a, b", "--port", "9")
    )
    (serial,) = sources
    assert isinstance(serial, SerialSource) and serial.decoder.keys == ["a", "b"]
    (web,) = outputs
    assert isinstance(web, WebDashboard) and web.port == 9


def test_build_zmq_pull_and_terminal(monkeypatch):
    monkeypatch.setattr("sys.stdin", Terminal())
    _, sources, outputs = build(args("--zmq", "tcp://*:5557", "--pull", "--terminal"))
    (zmq_source,) = sources
    assert isinstance(zmq_source, ZmqSource)
    assert (zmq_source.pattern, zmq_source.bind) == ("pull", True)
    assert isinstance(outputs[0], TerminalDisplay)


def test_build_reads_piped_stdin(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("a=1\n"))  # a pipe
    _, sources, _ = build(args())
    assert isinstance(sources[0], StdinSource)


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--csv", "a,b"], "--csv, --json and --binary describe a --serial device"),
        (["--zmq", "tcp://x:1", "--binary", "1=X"], "--serial device"),
        (["--pull"], "--pull goes with --zmq"),
        (["--serial", "auto", "--binary", "X=1"], "BYTE=NAME"),
        (["--demo", "launch", "--title", "X"], "its own page and data; drop --title"),
    ],
)
def test_bad_combinations_are_explained(argv, message, capsys):
    from sightglass.cli import main

    assert main([*argv, "--port", "0"]) == 1
    assert message in capsys.readouterr().err


def run_cli(*argv, stdin=subprocess.DEVNULL, cwd=None, env=None):
    """Start the command; return it and the dashboard URL it prints."""
    process = subprocess.Popen(
        [sys.executable, "-m", "sightglass", "--port", "0", *argv],
        stdin=stdin,
        stdout=subprocess.PIPE,
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


async def snapshot(url):
    async with aiohttp.ClientSession() as session, session.ws_connect(f"{url}ws") as ws:
        return (await ws.receive_json(timeout=5))["values"]


def stop(process):
    process.terminate()
    if process.stdin is not None and process.stdin.closed:
        process.stdin = None  # Python < 3.13's communicate() chokes on it
    return process.communicate(timeout=10)


def test_pipe_a_programs_output():
    process, url = run_cli(stdin=subprocess.PIPE)
    try:
        process.stdin.write("starting up\n")  # ordinary output: printed as-is
        process.stdin.write("progress=5 status=running\n")
        process.stdin.write('{"job": {"items": 12}}\n')
        process.stdin.close()  # the program ends; the dashboard stays up
        time.sleep(0.5)
        values = asyncio.run(snapshot(url))
        assert values == {"progress": "5", "status": "running", "job.items": "12"}
    finally:
        out, err = stop(process)
    assert out == "starting up\n"
    assert "input ended; still showing the last values" in err


def test_post_with_curl_like_request():
    process, url = run_cli()
    try:
        request = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             "-d", "temperature=21.5", f"{url}update"],
            capture_output=True, text=True, check=False,
        )  # fmt: skip
        if request.returncode != 0:
            pytest.skip("curl not available")
        assert request.stdout == "204"
        assert asyncio.run(snapshot(url)) == {"temperature": "21.5"}
    finally:
        stop(process)


def test_demo():
    process, url = run_cli("--demo")
    try:
        time.sleep(0.3)
        values = asyncio.run(snapshot(url))
        assert {"job.progress", "job.rate", "X.velocity", "machine", "error"} <= set(
            values
        )
        assert values["job.rate"]["values"]  # the chart has history
    finally:
        stop(process)


def test_demo_launch():
    """The showcase: its page, and values from all three of its programs."""
    process, url = run_cli("--demo", "launch")
    try:
        with urllib.request.urlopen(url) as page:
            assert "Aries II" in page.read().decode()
        deadline = time.monotonic() + 20  # the feeders are separate processes
        while time.monotonic() < deadline:
            values = asyncio.run(snapshot(url))
            if values["clock.time"] and values["weather.wind"]["values"]:
                break
            time.sleep(0.2)
        assert re.fullmatch(r"\d\d:\d\d:\d\d", values["clock.time"])  # ground
        assert values["weather.wind"]["values"]  # the weather mast
        assert int(values["frame"]) > 0  # the vehicle
    finally:
        # Stopped abruptly: the feeders notice and exit too, or this would
        # wait for them on the pipes they share.
        _, err = stop(process)
    assert "Ground systems (process" in err


def test_demo_launch_in_the_terminal(tmp_path):
    """--terminal draws the launch screen here, and still serves the page."""
    size = {"COLUMNS": "120", "LINES": "36", "PYTHONIOENCODING": "utf-8"}
    process, url = run_cli("--demo", "launch", "--terminal", cwd=tmp_path, env=size)
    try:
        # Nothing reads the terminal meanwhile, so its pipe fills and the
        # display stalls (as Ctrl+S would); the page must still be served.
        time.sleep(2)
        with urllib.request.urlopen(url, timeout=5) as page:
            assert "Aries II" in page.read().decode()
        # Stop it only once its feeder processes report: killed while they
        # are still starting, Windows children fail to attach to it.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            with urllib.request.urlopen(f"{url}values", timeout=5) as response:
                values = json.load(response)
            if values["clock.time"] and values["weather.wind"]["values"]:
                break
            time.sleep(0.2)
    finally:
        out, err = stop(process)
    log = tmp_path / "sightglass.log"
    logged = log.read_text() if log.exists() else "(no log)"
    report = f"values: {values}\nstderr: {err}\nlog: {logged}"
    assert values["clock.time"], report  # ground systems, from another process
    assert values["weather.wind"]["values"], report  # the weather mast, likewise
    assert out.startswith("\x1b[?1049h")  # the terminal's alternate screen
    for shown in ["Launch control", "Flight dynamics", "Go/no-go poll", "Events"]:
        assert shown in out
    assert "Traceback" not in err


def test_version():
    result = subprocess.run(
        [sys.executable, "-m", "sightglass", "--version"],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    assert result.stdout.startswith("sightglass 0.")


def test_busy_port_is_explained_for_the_command_line():
    process, url = run_cli()
    try:
        port = int(url.rsplit(":", 1)[1].strip("/"))
        result = subprocess.run(
            [sys.executable, "-m", "sightglass", "--port", str(port)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=20,
        )
    finally:
        stop(process)
    assert result.returncode == 1
    assert result.stderr == (
        f"sightglass: error: port {port} is already in use (is another dashboard "
        f"running?); choose a different port, e.g. --port {port + 1}\n"
    )


def test_no_input_note_when_stdin_is_empty():
    process, _ = run_cli()  # stdin is /dev/null, as under nohup or a service
    time.sleep(0.3)
    _, err = stop(process)
    assert "input ended" not in err
