"""The launch timeline that every program feeding the dashboard shares.

Each program works out where the launch is from the wall clock and the moment
the first countdown started, so they agree without talking to each other, as
real launch systems agree on one countdown clock. The launch repeats forever.
"""

import math
import time

COUNTDOWN = 30.0  # the clock starts at T-30 s

# Seconds after liftoff.
MECO = 100.0  # main engine cutoff: the first stage is spent
SEPARATION = 103.0
SES = 107.0  # second engine start
FAIRING = 140.0  # the nose fairing comes off, out of the atmosphere
SECO = 260.0  # second engine cutoff: in orbit
RECYCLE = 290.0  # and the countdown starts again

CYCLE = COUNTDOWN + RECYCLE


def mission_time(started: float) -> float:
    """Seconds from liftoff: negative during the countdown."""
    return (time.time() - started) % CYCLE - COUNTDOWN


def clock(t: float) -> str:
    """The countdown clock's reading, without its sign: ``"00:01:12"``."""
    seconds = math.ceil(-t) if t < 0 else int(t)  # T-0.4 still shows 00:00:01
    return f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}"


def stamp(t: float) -> str:
    """A mission time for the event log: ``"T+00:01:12"``."""
    return f"T{'-' if t < 0 else '+'}{clock(t)}"


def log_start(started: float) -> float:
    """Where a program that has just started begins its event log: a few
    seconds back, so what happened while it was starting isn't missed."""
    return mission_time(started) - 5
