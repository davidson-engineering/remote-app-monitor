"""The launch demo (``sightglass --demo launch``): its flight, clock and page."""

import re

import pytest

from sightglass.decoders import flatten
from sightglass.launch import HERE, build_monitor
from sightglass.launch import vehicle as flight
from sightglass.launch.mission import CYCLE, clock, stamp


def test_the_flight_reaches_orbit_through_max_q():
    orbit = flight.ORBIT
    assert orbit.altitude == pytest.approx(200_000, abs=5_000)
    assert orbit.velocity == pytest.approx(7_790, abs=50)
    assert 25 < flight.MAX_Q < 60
    peak = max(state.q for state in flight.FLIGHT)
    assert 30_000 < peak < 45_000  # Pa
    assert max(state.acceleration for state in flight.FLIGHT) <= flight.LIMIT + 0.01


def test_tanks_run_from_full_to_their_reserve():
    empty_1, empty_2 = flight.EMPTY
    assert flight.tank(1.0, empty_1, 0.02) == pytest.approx(100)
    assert flight.tank(empty_1, empty_1, 0.02) == pytest.approx(2)
    assert flight.tank(empty_2, empty_2, 0.03) == pytest.approx(3)


def test_the_log_names_each_flight_event_once_per_launch():
    events, before = [], -31.0
    for tenth in range(int(CYCLE * 10)):
        t = -30 + tenth / 10
        if event := flight.Vehicle.event(before, t, flight.state(t)):
            events.append(event[1].split(",")[0].split(" at ")[0].split(":")[0])
        before = t
    assert events == [
        "Engine ignition",
        "Liftoff",
        "Max-Q",
        "Main engine cutoff",
        "Stage separation",
        "Second engine start",
        "Fairing separation",
        "Orbit",
    ]


@pytest.mark.parametrize(
    ("t", "shown", "stamped"),
    [
        (-30, "00:00:30", "T-00:00:30"),
        (-0.4, "00:00:01", "T-00:00:01"),  # a countdown rounds up
        (0, "00:00:00", "T+00:00:00"),
        (72.9, "00:01:12", "T+00:01:12"),
    ],
)
def test_clock(t, shown, stamped):
    assert (clock(t), stamp(t)) == (shown, stamped)


def test_the_page_binds_only_values_the_demo_sends():
    """A mistyped data-bind would stay blank without complaint: check them all."""
    page = (HERE / "page.html").read_text("utf-8")
    bound = set(re.findall(r'data-bind="([^"]+)"', page))
    sent = set(flatten(build_monitor().snapshot()))
    assert bound
    assert bound <= sent, bound - sent
