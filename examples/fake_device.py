"""Pretend to be a microcontroller on a serial port (macOS and Linux).

Creates a pseudo-terminal, prints its path, and streams samples to it:

    python examples/fake_device.py             # CSV lines for robot_dashboard
    python examples/fake_device.py --binary    # binary frames for terminal_axes

Then point an example at the printed path, e.g.

    python examples/robot_dashboard/dashboard.py --port /dev/ttys012
"""

import argparse
import os
import struct
import time
import tty

from robot_dashboard.dashboard import FIELDS, simulate
from terminal_axes import NAMES, Simulator

IDS = {name: byte for byte, name in NAMES.items()}


def csv_sample(t: float) -> bytes:
    sample = simulate(t)
    return (",".join(sample[field] for field in FIELDS) + "\n").encode()


def frame(key: str, value: object) -> bytes:
    """Encode one update as a binary frame: the id, then params for the rest
    of the key ("X.velocity" is id X with param velocity)."""
    name, *params = key.split(".")
    if isinstance(value, str):
        payload = bytes([0x02]) + value.encode() + b"\x00"
    else:
        payload = bytes([0x01]) + struct.pack("<i", round(value * 1000))
    ids = bytes([len(params), *(IDS[p] for p in params)])
    return bytes([0xAA, IDS[name]]) + payload + ids + b"\xff"


def binary_sampler():
    simulator = Simulator()

    def sample(t: float) -> bytes:
        return b"".join(frame(key, value) for key, value in simulator(t).items())

    return sample


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--binary", action="store_true", help="send binary frames")
    parser.add_argument("--rate", type=float, default=50, help="samples per second")
    args = parser.parse_args()

    controller, device = os.openpty()
    tty.setraw(controller)
    tty.setraw(device)
    print(f"Fake device on {os.ttyname(device)} (Ctrl+C to stop)", flush=True)

    sample = binary_sampler() if args.binary else csv_sample
    start = time.monotonic()
    try:
        while True:
            os.write(controller, sample(time.monotonic() - start))
            time.sleep(1 / args.rate)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
