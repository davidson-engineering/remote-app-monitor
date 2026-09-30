"""Launch control: a rocket launch, live, fed by three separate programs.

    uv run --all-extras examples/launch_control/launch_control.py

The page, launch_control.html, is plain HTML and CSS: every value on it is
bound with a data-bind attribute, and its gauges, tanks and lamps are drawn
by CSS from those values. Three programs feed it, each in a different way:

- the vehicle's telemetry, 10 frames a second: a source inside this program
  (vehicle.py, simulated; SerialSource or ZmqSource would read a real one)
- ground systems (ground.py): the countdown clock, the go/no-go poll and the
  pad, sent with Client from another process
- the weather mast (weather.py): plain HTTP posts, no library at all

Stop a feeder and its part of the page says it has gone quiet. The launch
repeats every five minutes or so; stop with Ctrl+C.
"""

import argparse
import multiprocessing
import time
from pathlib import Path

import ground
import mission
import weather
from vehicle import ENGINES, EVENTS, Vehicle

from app_monitor import (
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--host", default="127.0.0.1", help="use 0.0.0.0 to share on your network"
    )
    parser.add_argument("--port", type=int, default=0, help="(default: any free one)")
    parser.add_argument(
        "--at",
        type=float,
        default=-mission.COUNTDOWN,
        metavar="SECONDS",
        help="start this long after liftoff (default: at the start of the countdown)",
    )
    args = parser.parse_args()

    # When the first countdown started; every feeder works from this.
    started = time.time() - mission.COUNTDOWN - args.at
    web = WebDashboard(
        HERE / "launch_control.html",
        static_dir=HERE / "static",
        host=args.host,
        port=args.port,
        open_browser=True,
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
        flush=True,
    )
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    monitor.stop()


if __name__ == "__main__":
    main()
