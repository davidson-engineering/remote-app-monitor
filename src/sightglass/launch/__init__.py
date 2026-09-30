"""Launch control: a rocket launch, live, fed by three separate programs.

    sightglass --demo launch --open

The page, page.html, is plain HTML and CSS: every value on it is bound with
a data-bind attribute, and its gauges, tanks and lamps are drawn by CSS from
those values. Three programs feed it, each in a different way:

- the vehicle's telemetry, 10 frames a second: a source inside this program
  (vehicle.py, simulated; SerialSource or ZmqSource would read a real one)
- ground systems (ground.py): the countdown clock, the go/no-go poll and the
  pad, sent with Client from another process
- the weather mast (weather.py): plain HTTP posts, no library at all

Stop a feeder and its part of the page says it has gone quiet. The launch
repeats every five minutes or so; stop with Ctrl+C. To build something
similar, copy this folder: it uses only sightglass's public API.
"""

import multiprocessing
import sys
import time
from pathlib import Path

from sightglass import (
    IndicatorLamp,
    LogMonitor,
    MachineState,
    Monitor,
    ProgressBar,
    RangeBar,
    SimulatedSource,
    Sparkline,
    TextElement,
    TextFormat,
    WebDashboard,
)

from . import ground, weather
from .mission import COUNTDOWN
from .vehicle import ENGINES, EVENTS, Vehicle

HERE = Path(__file__).parent


def fixed(precision: int) -> TextFormat:
    return TextFormat(precision=precision)


def build_monitor() -> Monitor:
    # strict: this page shows a fixed set of values, so a mistyped id is
    # reported instead of quietly becoming a new element.
    monitor = Monitor(strict=True)

    # The vehicle. Units are on the page, so the values are bare numbers.
    monitor.add(
        TextElement("frame", format=TextFormat(width=6, padding="0")),
        # One chart point a second for five minutes: the whole flight.
        Sparkline("altitude", points=300, interval=1, format=fixed(1)),
        Sparkline("velocity", points=300, interval=1, format=fixed(0)),
        TextElement("downrange", format=fixed(0)),
        TextElement("stage"),
        RangeBar("q", max_value=40, precision=1),  # dynamic pressure, kPa
        RangeBar("acceleration", max_value=5, precision=2),  # g
        RangeBar("throttle", max_value=100, precision=0),  # %
        TextElement("pitch", format=fixed(1)),
        MachineState("engines", states=ENGINES),
        MachineState("events", states=EVENTS),
    )
    monitor.add_group(
        "propellant",
        [ProgressBar(tank) for tank in ("s1_lox", "s1_fuel", "s2_lox", "s2_fuel")],
    )

    # Ground systems.
    monitor.add_group("clock", [TextElement("time"), IndicatorLamp("counting_up")])
    monitor.add(TextElement("status"))
    monitor.add_group("poll", [IndicatorLamp(console) for console in ground.POLL])
    monitor.add_group(
        "pad",
        [IndicatorLamp("loaded"), IndicatorLamp("strongback"), IndicatorLamp("deluge")],
    )

    # The weather mast.
    monitor.add_group(
        "weather",
        [
            Sparkline("wind", points=120, format=fixed(0)),  # kt, two minutes
            TextElement("gust", format=fixed(0)),
            TextElement("direction", format=fixed(0)),  # degrees, wind from
            TextElement("temperature", format=fixed(1)),  # °C
        ],
    )

    # Written by both the vehicle and the ground.
    monitor.add(LogMonitor("log", lines=8))
    return monitor


def run(
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
    at: float = -COUNTDOWN,
    open_browser: bool = False,
) -> None:
    """Serve the launch until Ctrl+C, starting ``at`` seconds from liftoff."""
    # When the first countdown started; every feeder works from this.
    started = time.time() - COUNTDOWN - at
    web = WebDashboard(
        HERE / "page.html",
        static_dir=HERE / "static",
        host=host,
        port=port,
        open_browser=open_browser,
    )
    monitor = build_monitor()
    monitor.start(sources=[SimulatedSource(Vehicle(started), rate=10)], outputs=[web])

    feeders = [
        multiprocessing.Process(target=ground.run, args=(web.url, started)),
        multiprocessing.Process(target=weather.run, args=(web.url,)),
    ]
    for feeder in feeders:
        feeder.daemon = True  # stopped with this program
        feeder.start()
    print(
        f"Ground systems (process {feeders[0].pid}) and the weather mast "
        f"(process {feeders[1].pid}) report to it; stop either to see its part "
        "of the page go quiet. Ctrl+C stops everything.",
        file=sys.stderr,
        flush=True,
    )
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    monitor.stop()
