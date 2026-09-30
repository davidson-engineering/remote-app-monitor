"""The app-monitor command, run the way a user would."""

import asyncio
import io
import os
import subprocess
import sys
import time

import aiohttp
import pytest

from app_monitor import (
    SerialSource,
    StdinSource,
    TerminalDisplay,
    WebDashboard,
    ZmqSource,
)
from app_monitor.cli import build, parser


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
    ],
)
def test_bad_combinations_are_explained(argv, message, capsys):
    from app_monitor.cli import main

    assert main([*argv, "--port", "0"]) == 1
    assert message in capsys.readouterr().err


def run_cli(*argv, stdin=subprocess.DEVNULL):
    """Start the command; return it and the dashboard URL it prints."""
    process = subprocess.Popen(
        [sys.executable, "-m", "app_monitor", "--port", "0", *argv],
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
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


def test_version():
    result = subprocess.run(
        [sys.executable, "-m", "app_monitor", "--version"],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    assert result.stdout.startswith("app-monitor 0.")


def test_busy_port_is_explained_for_the_command_line():
    process, url = run_cli()
    try:
        port = int(url.rsplit(":", 1)[1].strip("/"))
        result = subprocess.run(
            [sys.executable, "-m", "app_monitor", "--port", str(port)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=20,
        )
    finally:
        stop(process)
    assert result.returncode == 1
    assert result.stderr == (
        f"app-monitor: error: port {port} is already in use (is another dashboard "
        f"running?); choose a different port, e.g. --port {port + 1}\n"
    )


def test_no_input_note_when_stdin_is_empty():
    process, _ = run_cli()  # stdin is /dev/null, as under nohup or a service
    time.sleep(0.3)
    _, err = stop(process)
    assert "input ended" not in err
