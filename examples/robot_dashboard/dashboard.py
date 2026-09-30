"""Robot control panel: a web dashboard fed by a microcontroller over serial.

    python examples/robot_dashboard/dashboard.py              # first USB serial device
    python examples/robot_dashboard/dashboard.py --port /dev/ttyUSB0
    python examples/robot_dashboard/dashboard.py --simulate   # no hardware needed

Then open http://127.0.0.1:8080/. The device sends one CSV line per sample with
the values in the order of FIELDS; see examples/fake_device.py for a stand-in.
"""

import argparse
import logging
import math
from pathlib import Path

from sightglass import (
    CsvDecoder,
    MachineState,
    Monitor,
    SerialSource,
    SimulatedSource,
    TextElement,
    TextFormat,
    WebDashboard,
)

HERE = Path(__file__).parent

MOTORS = range(1, 5)

FIELDS = [
    "position_x",
    "position_y",
    "position_z",
    *(f"motor{m}_{field}" for m in MOTORS for field in ("speed", "torque", "status")),
    "machine_status",
]

MACHINE_STATES = [  # bit 0 first
    "deadman_switch",
    "motors_enabled",
    "emergency_stop",
    "rapid_traverse",
    "fine_control",
    "machine_mode",
]

# "+00012.345": fills the 10-character position display so digits sit on the grid.
POSITION = TextFormat(width=10, precision=3, force_sign=True, padding="0")
SPEED = TextFormat(precision=0)
TORQUE = TextFormat(precision=1, force_sign=True)


def build_monitor() -> Monitor:
    monitor = Monitor()
    for axis in "xyz":
        monitor.add(TextElement(f"position_{axis}", format=POSITION))
    for m in MOTORS:
        monitor.add(
            TextElement(f"motor{m}_speed", format=SPEED),
            TextElement(f"motor{m}_torque", format=TORQUE),
            TextElement(f"motor{m}_status"),
        )
    monitor.add(MachineState("machine_status", states=MACHINE_STATES))
    return monitor


def simulate(t: float) -> dict[str, str]:
    """One sample, as the device would send it (all strings)."""
    x, y, z = (
        120 * math.sin(0.5 * t),
        80 * math.cos(0.3 * t),
        -15 + 5 * math.sin(0.8 * t),
    )
    sample = {
        "position_x": f"{x:.3f}",
        "position_y": f"{y:.3f}",
        "position_z": f"{z:.3f}",
    }
    for m in MOTORS:
        speed = 1500 * math.sin(0.5 * t + m)
        sample[f"motor{m}_speed"] = f"{speed:.0f}"
        sample[f"motor{m}_torque"] = f"{abs(speed) / 120 + math.sin(7 * t):.2f}"
        sample[f"motor{m}_status"] = "WARN" if abs(speed) > 1450 else "OK"
    cycle = int(t) % 12
    states = {
        "deadman_switch": cycle < 10,
        "motors_enabled": 1 <= cycle < 10,
        "emergency_stop": cycle >= 10,
        "rapid_traverse": 3 <= cycle < 6,
        "fine_control": 6 <= cycle < 9,
        "machine_mode": True,
    }
    bits = sum(1 << i for i, name in enumerate(MACHINE_STATES) if states[name])
    sample["machine_status"] = str(bits)
    return sample


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--port", default="auto", help="serial device (default: auto)")
    source.add_argument("--simulate", action="store_true", help="generate fake data")
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument(
        "--host", default="127.0.0.1", help="use 0.0.0.0 for LAN access"
    )
    parser.add_argument("--http-port", type=int, default=8080)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    if args.simulate:
        feed = SimulatedSource(simulate, rate=20)
    else:
        feed = SerialSource(args.port, args.baudrate, decoder=CsvDecoder(FIELDS))
    dashboard = WebDashboard(
        HERE / "dashboard.html",
        static_dir=HERE / "static",
        host=args.host,
        port=args.http_port,
        stale_after=2,  # the device streams continuously; silence means trouble
    )
    build_monitor().serve(sources=[feed], outputs=[dashboard])


if __name__ == "__main__":
    main()
