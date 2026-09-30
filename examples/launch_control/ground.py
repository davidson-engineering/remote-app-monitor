"""Ground systems: the countdown clock, the go/no-go poll and the pad.

A separate program that reports to the dashboard with Client, which needs
nothing but the standard library, as launch control software running on
another machine would. launch_control.py starts it; to run it yourself:

    python ground.py http://127.0.0.1:8080 <countdown start, as time.time()>
"""

import argparse
import math
import multiprocessing
import time

from mission import MECO, SECO, SEPARATION, clock, log_start, mission_time, stamp

from sightglass import Client

# Each console answers the launch director's poll, one a second.
POLL = {
    "range": -29,
    "weather": -28,
    "propulsion": -27,
    "guidance": -26,
    "recovery": -25,
    "launch_director": -24,
}
GO_FOR_LAUNCH = -22
STRONGBACK = -15  # the arm holding the rocket swings back
DELUGE = (-8, 15)  # water floods the pad to soak up the noise of the engines

STATUS = [  # (from, what the launch director would say)
    (-30, "Polling for launch"),
    (GO_FOR_LAUNCH, "Go for launch"),
    (-10, "Terminal count"),
    (0, "Liftoff"),
    (10, "Vehicle has cleared the tower"),
    (30, "Flight is nominal"),
    (SEPARATION, "Stage separation confirmed"),
    (SEPARATION + 10, "Second stage burning"),
    (SECO, "Orbit achieved"),
]

MESSAGES = [
    (-30, "Countdown started"),
    *((at, f"{name.replace('_', ' ').capitalize()}: go") for name, at in POLL.items()),
    (GO_FOR_LAUNCH, "Launch director: go for launch"),
    (STRONGBACK, "Strongback retracted"),
    (DELUGE[0], "Water deluge on"),
    (10, "Tower cleared"),
    (DELUGE[1], "Water deluge off"),
    (MECO + 60, "First stage is on its way home"),
]


def report(t: float) -> dict:
    """Everything the ground systems show at ``t`` seconds from liftoff."""
    return {
        "clock.time": clock(t),
        "clock.counting_up": t >= 0,
        "status": next(text for at, text in reversed(STATUS) if t >= at),
        **{f"poll.{name}": t >= at for name, at in POLL.items()},
        "pad.loaded": t < 0,
        "pad.strongback": t >= STRONGBACK,
        "pad.deluge": DELUGE[0] <= t < DELUGE[1],
    }


def run(url: str, started: float) -> None:
    dashboard = Client(url)
    parent = multiprocessing.parent_process()  # None when run on its own
    before = log_start(started)
    try:
        while parent is None or parent.is_alive():
            t = mission_time(started)
            if t < before:  # the launch has started over
                before = -math.inf
            dashboard.update(report(t))
            for at, message in MESSAGES:
                if before < at <= t:
                    dashboard.set("log", f"{stamp(at)}  {message}")
            before = t
            time.sleep(0.1)
    except KeyboardInterrupt:  # Ctrl+C reaches every process
        pass
    finally:
        dashboard.close(timeout=0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("url", help="the dashboard's address")
    parser.add_argument("started", type=float, help="when the first countdown began")
    args = parser.parse_args()
    run(args.url, args.started)
