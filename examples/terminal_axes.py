"""Terminal dashboard for a three-axis machine.

    python examples/terminal_axes.py --simulate
    python examples/terminal_axes.py --zmq tcp://localhost:5556  # zmq_publisher.py
    python examples/terminal_axes.py --port /dev/ttyUSB0         # binary frames

Over serial the device speaks the binary frame protocol (see
BinaryFrameDecoder): the frame id names the axis and a param names the value,
so id "X" with param "velocity" updates element "X.velocity".
"""

import argparse
import asyncio
import contextlib
import logging
import math

from app_monitor import (
    BinaryFrameDecoder,
    Coordinate,
    LogMonitor,
    Monitor,
    RangeBar,
    SerialSource,
    SimulatedSource,
    Style,
    TerminalDisplay,
    ZmqSource,
)

AXES = ["X", "Y", "Z"]

# Byte values the firmware uses for ids and params.
NAMES = {
    1: "X",
    2: "Y",
    3: "Z",
    4: "position",
    5: "log",
    10: "velocity",
    11: "torque",
    20: "x",
    21: "y",
    22: "z",
}


def build_monitor() -> Monitor:
    monitor = Monitor()
    monitor.add(
        Coordinate("position", label="Position", units="mm", style=Style(bold=True))
    )
    bar = {"min_value": -10, "max_value": 10, "precision": 1, "marker": "█"}
    per_axis = [
        RangeBar("velocity", units="mm/s", bar_style=Style(fg="green"), **bar),
        RangeBar("torque", units="Nm", bar_style=Style(fg="yellow"), **bar),
    ]
    for axis in AXES:
        monitor.add_group(axis, per_axis)  # copied: X.velocity, Y.velocity, ...
    monitor.add(LogMonitor("log", lines=5, timestamp=True, border=True))
    return monitor


class Simulator:
    """Fake readings for --simulate (fake_device.py sends the same, as frames)."""

    def __init__(self) -> None:
        self.checkpoint = -1

    def __call__(self, t: float) -> dict[str, object]:
        sample: dict[str, object] = {}
        for i, axis in enumerate(AXES):
            velocity = 8 * math.sin(0.7 * t + i)
            sample[f"{axis}.velocity"] = velocity
            sample[f"{axis}.torque"] = velocity * 0.6 + math.sin(5 * t)
            sample[f"position.{axis.lower()}"] = 50 * math.sin(0.2 * t + i)
        if int(t // 2) != self.checkpoint:  # a log line every 2 seconds
            self.checkpoint = int(t // 2)
            sample["log"] = f"checkpoint at t={t:.1f}s"
        return sample


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--simulate", action="store_true", help="generate fake data")
    source.add_argument("--zmq", metavar="ENDPOINT", help="ZeroMQ publisher to read")
    source.add_argument("--port", help="serial device sending binary frames")
    parser.add_argument("--baudrate", type=int, default=115200)
    args = parser.parse_args()

    # Log to a file: anything printed to the terminal would be drawn over.
    logging.basicConfig(filename="terminal_axes.log", level=logging.INFO)
    if args.simulate:
        feed = SimulatedSource(Simulator(), rate=30)
    elif args.zmq:
        feed = ZmqSource(args.zmq)
    else:
        feed = SerialSource(args.port, args.baudrate, decoder=BinaryFrameDecoder(NAMES))
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(build_monitor().run(sources=[feed], outputs=[TerminalDisplay()]))


if __name__ == "__main__":
    main()
