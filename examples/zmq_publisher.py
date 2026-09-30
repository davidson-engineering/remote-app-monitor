# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pyzmq>=26",
# ]
# ///
"""Publish sample values over ZeroMQ for terminal_axes.py --zmq to display.

    python examples/zmq_publisher.py
    python examples/terminal_axes.py --zmq tcp://localhost:5556

Each message is "<element id> <value>", the format ZmqSource expects by default.
"""

import math
import time

import zmq


def main(endpoint: str = "tcp://*:5556", rate: float = 30) -> None:
    socket = zmq.Context.instance().socket(zmq.PUB)
    socket.bind(endpoint)
    print(f"Publishing on {endpoint} (Ctrl+C to stop)")
    start = time.monotonic()
    try:
        while True:
            t = time.monotonic() - start
            for i, axis in enumerate("XYZ"):
                velocity = 8 * math.sin(0.7 * t + i)
                socket.send_string(f"{axis}.velocity {velocity:.3f}")
                socket.send_string(f"{axis}.torque {velocity * 0.6:.3f}")
                socket.send_string(
                    f"position.{axis.lower()} {50 * math.sin(0.2 * t + i):.3f}"
                )
            if int(t * rate) % (2 * int(rate)) == 0:
                socket.send_string(f"log checkpoint at t={t:.1f}s")
            time.sleep(1 / rate)
    except KeyboardInterrupt:
        pass
    finally:
        socket.close()


if __name__ == "__main__":
    main()
