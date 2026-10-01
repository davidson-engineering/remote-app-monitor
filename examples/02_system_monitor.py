# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "sightglass[web]",
#     "psutil>=5.9",
# ]
# ///
"""System monitor: this computer's CPU, memory, disk and network, live.

    uv run https://raw.githubusercontent.com/davidson-engineering/sightglass/main/examples/02_system_monitor.py

Shows how to choose the displays yourself: charts for trends, bars for
capacity, a table, and groups. Real data, from psutil. Stop with Ctrl+C.
"""

import socket
import time
from pathlib import Path

import psutil

from sightglass import (
    Monitor,
    ProgressBar,
    Sparkline,
    Table,
    TextElement,
    TextFormat,
    WebDashboard,
)

ONE_DECIMAL = TextFormat(precision=1)
TOP = ["1", "2", "3", "4", "5"]

monitor = Monitor()
monitor.add_group(
    "CPU",
    [
        Sparkline("usage", label="Usage", units="%", format=ONE_DECIMAL),
        TextElement("load", label="Load average"),
    ],
)
monitor.add_group(
    "Memory",
    [
        ProgressBar("used", label="Used"),
        TextElement("detail", label="In use"),
    ],
)
monitor.add_group(
    "Disk",
    [ProgressBar("used", label="Used"), TextElement("free", label="Free")],
)
monitor.add_group(
    "Network",
    [
        # These receive bytes per second; scale shows them as KB/s.
        Sparkline(
            "received",
            label="Received",
            units="KB/s",
            scale=1 / 1024,
            format=ONE_DECIMAL,
        ),
        Sparkline(
            "sent", label="Sent", units="KB/s", scale=1 / 1024, format=ONE_DECIMAL
        ),
    ],
)
monitor.add(
    Table(
        "top",
        label="Busiest processes",
        rows=TOP,
        columns=["process", "cpu %", "memory %"],
    )
)
monitor.start(
    outputs=[WebDashboard(title=socket.gethostname(), port=0, open_browser=True)]
)


def gigabytes(count: float) -> str:
    return f"{count / 1024**3:.1f} GB"


network = psutil.net_io_counters()
then = time.monotonic()
try:
    while True:
        time.sleep(1)
        now, latest = time.monotonic(), psutil.net_io_counters()
        seconds = now - then
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage(str(Path.home()))  # the volume with your files
        busiest = sorted(
            (
                p.info
                for p in psutil.process_iter(["name", "cpu_percent", "memory_percent"])
            ),
            key=lambda info: info["cpu_percent"] or 0,
            reverse=True,
        )[: len(TOP)]
        update = {
            "CPU.usage": psutil.cpu_percent(),
            "CPU.load": " ".join(f"{load:.2f}" for load in psutil.getloadavg()),
            "Memory.used": memory.percent,
            "Memory.detail": f"{gigabytes(memory.used)} of {gigabytes(memory.total)}",
            "Disk.used": disk.percent,
            "Disk.free": gigabytes(disk.free),
            "Network.received": (latest.bytes_recv - network.bytes_recv) / seconds,
            "Network.sent": (latest.bytes_sent - network.bytes_sent) / seconds,
        }
        for row, info in zip(TOP, busiest, strict=False):
            update[f"top.{row}.process"] = info["name"] or "?"
            update[f"top.{row}.cpu %"] = f"{info['cpu_percent'] or 0:.1f}"
            update[f"top.{row}.memory %"] = f"{info['memory_percent'] or 0:.1f}"
        monitor.update(update)
        network, then = latest, now
except KeyboardInterrupt:
    pass
