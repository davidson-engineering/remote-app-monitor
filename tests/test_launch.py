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


# -- in a terminal ----------------------------------------------------------------


@pytest.fixture
def flown(monkeypatch):
    """A monitor that has seen the launch up to T+52, as the demo would."""
    from sightglass.launch import ground

    clock = {"now": 0.0, "mission": -30.0}
    monkeypatch.setattr("sightglass.elements.monotonic", lambda: clock["now"])
    monkeypatch.setattr(flight, "mission_time", lambda started: clock["mission"])
    monkeypatch.setattr(flight, "log_start", lambda started: -31)
    monitor, rocket, before = build_monitor(), flight.Vehicle(0), -31.0
    for tenth in range(-300, 521):
        t = tenth / 10
        clock["now"], clock["mission"] = t + 30, t
        monitor.update(rocket(0), ground.report(t))
        for at, message in ground.MESSAGES:
            if before < at <= t:
                monitor.set("log", f"{stamp(at)}  {message}")
        if tenth % 10 == 0:
            weather = {"wind": 11 + tenth % 7, "gust": 15, "direction": 240}
            monitor.update({f"weather.{k}": v for k, v in weather.items()})
        before = t
    return monitor


@pytest.mark.parametrize(
    "size", [(160, 50), (120, 36), (101, 34), (100, 40), (80, 24), (64, 20)]
)
def test_terminal_screen_fits_the_terminal(flown, size):
    from sightglass.launch.screen import plain, screen, width_of

    width, height = size
    frame = screen(flown, width, height, "http://127.0.0.1:8080/")
    lines = frame.split("\n")
    assert len(lines) <= height
    assert max(map(width_of, lines)) <= width - 1  # the last column stays free
    text = plain(frame)
    for shown in ["Aries II", "Flight is nominal", "Max-Q, 37.6 kPa", "14.3", "546"]:
        assert shown in text, (shown, size)


def test_terminal_screen_is_polite_when_too_small(flown):
    from sightglass.launch.screen import plain, screen

    assert "at least 60 x 20" in plain(screen(flown, 50, 30))


@pytest.mark.parametrize("size", [(120, 36), (80, 24)])
def test_terminal_screen_says_which_feed_went_quiet(flown, monkeypatch, size):
    from sightglass.launch.screen import plain, screen

    ages = {**flown.ages(), "clock.time": 60.0, "weather.wind": None}
    monkeypatch.setattr(flown, "ages", lambda: ages)
    text = plain(screen(flown, *size))
    assert "No data from ground" in text  # beside the clock it may wrap
    assert "Flight is nominal" not in text  # the ground's status: replaced
    assert "14.3" in text  # the vehicle is fine


def test_segment_clock():
    from sightglass.launch.screen import plain, segment_clock

    rows = [plain(row) for row in segment_clock("12:34:56")]
    assert len(rows) == 5
    assert {len(row) for row in rows} == {6 * 8 + 2 * 3}
