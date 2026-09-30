"""The launch in a terminal: ``sightglass --demo launch --terminal``.

A screen for TerminalDisplay, the terminal's counterpart of page.html: it lays
out the whole frame from the monitor's values, in xterm's 256 colors. The
look follows the page: an amber segment clock, cyan telemetry, green for
running and go, amber for no data.

Two layouts: the full one from 101 x 34, and a compact one for smaller
terminals down to the classic 80 x 24 (and, more tersely, 60 x 20).
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence

from sightglass import Monitor, Style

from .ground import POLL
from .vehicle import ENGINES, EVENTS

# xterm-256 colors.
CLOCK, UNLIT = 214, 236
CLOCK_QUIET, UNLIT_QUIET = 94, 235  # the last reading, dimmed
TELEMETRY, GO, CAUTION = 81, 78, 221
TEXT, BRIGHT, LEGEND, RULE, OFF = 252, 255, 245, 238, 238

EVENT_NAMES = {
    "liftoff": "Liftoff",
    "maxq": "Max-Q",
    "meco": "Main engine cutoff",
    "separation": "Stage separation",
    "ses": "Second engine start",
    "fairing": "Fairing separation",
    "seco": "Orbit",
}
SHORT_EVENTS = {
    "liftoff": "Liftoff",
    "maxq": "Max-Q",
    "meco": "MECO",
    "separation": "Sep",
    "ses": "SES",
    "fairing": "Fairing",
    "seco": "Orbit",
}
CONSOLES = {name: name.replace("_", " ").capitalize() for name in POLL}
FEEDS = {  # the value each feed sends every time, and how long it may pause
    "vehicle": ("frame", 1.0, "the vehicle"),
    "ground": ("clock.time", 2.0, "ground systems"),
    "weather": ("weather.wind", 3.0, "the weather mast"),
}


def paint(color: int, text: str, *, bold: bool = False) -> str:
    return Style(fg=color, bold=bold).apply(text) if text else ""


def lamp(on: bool, color: int = GO) -> str:
    return paint(color if on else OFF, "●")


# -- text layout ----------------------------------------------------------------

_ANSI = re.compile(r"(\x1b\[[0-9;]*m)")


def width_of(line: str) -> int:
    return len(_ANSI.sub("", line))


def fit(line: str, width: int) -> str:
    """Exactly ``width`` columns: padded, or cut without losing its colors."""
    shown, out = 0, []
    for part in _ANSI.split(line):
        if part.startswith("\x1b["):
            out.append(part)
            continue
        room = width - shown
        out.append(part[:room])
        shown += min(len(part), room)
    return "".join(out) + "\x1b[0m" * (shown == width) + " " * (width - shown)


def plain(line: str) -> str:
    return _ANSI.sub("", line)


def side_by_side(
    blocks: Sequence[list[str]], widths: Sequence[int], gap: str
) -> list[str]:
    height = max(len(block) for block in blocks)
    rows = []
    for i in range(height):
        cells = [
            fit(block[i] if i < len(block) else "", width)
            for block, width in zip(blocks, widths, strict=True)
        ]
        rows.append(gap.join(cells))
    return rows


def spread(left: str, right: str, width: int) -> str:
    """``left`` and ``right`` on one line, pushed to its ends."""
    space = width - width_of(left) - width_of(right)
    return left + " " * max(space, 1) + right if space >= 1 else fit(left, width)


# -- pixels: half blocks and braille ------------------------------------------------


def half_blocks(
    pixels: dict[tuple[int, int], int], width: int, height: int
) -> list[str]:
    """Colored pixels, two per character cell (top and bottom half)."""
    rows = []
    for top in range(0, height, 2):
        cells = []
        for x in range(width):
            upper, lower = pixels.get((x, top)), pixels.get((x, top + 1))
            if upper is None and lower is None:
                cells.append(" ")
            elif lower is None:
                cells.append(paint(upper, "▀"))
            elif upper is None:
                cells.append(paint(lower, "▄"))
            elif upper == lower:
                cells.append(paint(upper, "█"))
            else:
                cells.append(Style(fg=upper, bg=lower).apply("▀"))
        rows.append("".join(cells))
    return rows


# The segments of a 6 x 9 pixel digit, with gaps at the joints like a real
# display. A pixel is half a character cell: about square.
_SEGMENTS = {
    "a": [(x, 0) for x in range(1, 5)],
    "b": [(5, y) for y in range(1, 4)],
    "c": [(5, y) for y in range(5, 8)],
    "d": [(x, 8) for x in range(1, 5)],
    "e": [(0, y) for y in range(5, 8)],
    "f": [(0, y) for y in range(1, 4)],
    "g": [(x, 4) for x in range(1, 5)],
}
_LIT = {
    "0": "abcdef",
    "1": "bc",
    "2": "abdeg",
    "3": "abcdg",
    "4": "bcfg",
    "5": "acdfg",
    "6": "acdefg",
    "7": "abc",
    "8": "abcdefg",
    "9": "abcdfg",
}


def segment_clock(text: str, *, quiet: bool = False) -> list[str]:
    """``"00:01:12"`` as a segment display, 5 rows tall, unlit segments
    showing; dimmed when ``quiet`` (its source stopped), still readable."""
    lit, unlit = (CLOCK_QUIET, UNLIT_QUIET) if quiet else (CLOCK, UNLIT)
    pixels: dict[tuple[int, int], int] = {}
    x = 0
    for char in text:
        if char == ":":  # a dot column, then the usual gap
            pixels[(x, 2)] = pixels[(x, 6)] = lit
            x += 3
            continue
        for name, points in _SEGMENTS.items():
            color = lit if name in _LIT.get(char, "") else unlit
            for px, py in points:
                pixels[(x + px, py)] = color
        x += 8  # 6 wide, then a gap of 2
    return half_blocks(pixels, x, 9)


class Braille:
    """A dot canvas: each character cell holds 2 x 4 dots."""

    _BITS = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))

    def __init__(self, cols: int, rows: int) -> None:
        self.cols, self.rows = cols, rows
        self.width, self.height = cols * 2, rows * 4
        self.cells = [[0] * cols for _ in range(rows)]

    def dot(self, x: float, y: float) -> None:
        x, y = round(x), round(y)
        if 0 <= x < self.width and 0 <= y < self.height:
            self.cells[y // 4][x // 2] |= self._BITS[y % 4][x % 2]

    def line(self, x0: float, y0: float, x1: float, y1: float) -> None:
        steps = max(abs(x1 - x0), abs(y1 - y0), 1)
        for i in range(int(steps) + 1):
            self.dot(x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps)

    def lines(self, color: int) -> list[str]:
        return [
            paint(color, "".join(chr(0x2800 + c) if c else " " for c in row))
            for row in self.cells
        ]


def chart(values: Iterable[float], cols: int, rows: int) -> list[str]:
    """An area chart of ``values``, ``cols`` x ``rows`` characters, scaled to
    fit like the page's sparklines (a steady value sits mid-height)."""
    canvas = Braille(cols, rows)
    points = list(values)
    if len(points) >= 2:
        low, high = min(points), max(points)
        span, last, bottom = high - low, len(points) - 1, canvas.height - 1
        for x in range(canvas.width):
            value = points[round(x * last / (canvas.width - 1))]
            top = bottom * (1 - (value - low) / span) if span else canvas.height / 2
            canvas.line(x, top, x, bottom)
    return canvas.lines(TELEMETRY)


def gauge(ratio: float | None, width: int) -> str:
    """A bar filled to ``ratio``, to half a character."""
    halves = round(max(0.0, min(ratio or 0.0, 1.0)) * width * 2)
    full, half = divmod(halves, 2)
    filled = "━" * full + "╸" * half
    return paint(TELEMETRY, filled) + paint(RULE, "━" * (width - full - half))


def tank(ratio: float | None, rows: int) -> list[str]:
    """A two-column tank, filled to ``ratio`` in eighths of a row."""
    eighths = round(max(0.0, min(ratio or 0.0, 1.0)) * rows * 8)
    lines = []
    for row in range(rows - 1, -1, -1):  # top first
        fill = max(0, min(eighths - row * 8, 8))
        lines.append(
            paint(TELEMETRY, " ▁▂▃▄▅▆▇█"[fill] * 2) if fill else paint(UNLIT, "░░")
        )
    return lines


def compass(direction: float | None, cols: int = 9, rows: int = 4) -> list[str]:
    """A dial with an arrow pointing where the wind blows (from ``direction``)."""
    canvas = Braille(cols, rows)
    cx, cy = canvas.width / 2 - 0.5, canvas.height / 2 - 0.5
    radius = canvas.height / 2 - 0.5
    for degrees in range(0, 360, 30):
        a = math.radians(degrees)
        canvas.dot(cx + math.sin(a) * radius, cy - math.cos(a) * radius)
    if direction is not None:
        a = math.radians(direction + 180)
        tip = (cx + math.sin(a) * radius * 0.75, cy - math.cos(a) * radius * 0.75)
        canvas.line(
            cx - math.sin(a) * radius * 0.55, cy + math.cos(a) * radius * 0.55, *tip
        )
    return canvas.lines(TELEMETRY)


# -- reading the monitor ------------------------------------------------------------


def number(monitor: Monitor, id: str) -> float | None:
    try:
        return float(monitor[id].value)
    except (TypeError, ValueError):
        return None


def reading(monitor: Monitor, id: str, units: str) -> str:
    """A value in cyan, then its units ("12 kt", but "240°")."""
    text = paint(TELEMETRY, monitor[id].text.strip(), bold=True)
    if not units:
        return text
    return text + paint(LEGEND, units if units == "°" else f" {units}")


def quiet_feeds(monitor: Monitor) -> dict[str, bool]:
    ages = monitor.ages()
    return {
        feed: ages[id] is None or ages[id] > limit
        for feed, (id, limit, _) in FEEDS.items()
    }


# -- panels -------------------------------------------------------------------------


def panel(
    title: str, body: list[str], width: int, quiet: str | None = None
) -> list[str]:
    """A titled panel; dimmed, and saying so, when its feed has gone quiet."""
    heading = paint(LEGEND, title)
    if quiet:
        note = f"No data from {quiet}"
        if width_of(heading) + 1 + len(note) > width:
            note = "No data"
        heading = spread(heading, paint(CAUTION, note), width)
        body = [paint(OFF, plain(line)) for line in body]
    return [fit(heading, width), *(fit(line, width) for line in body)]


def masthead(monitor: Monitor, quiet: dict[str, bool], width: int) -> str:
    """The title, and a lamp per feed: amber when it has gone quiet."""
    title = (
        paint(BRIGHT, "Aries II", bold=True) + "  " + paint(LEGEND, "Launch control")
    )
    lamps = {feed: lamp(True, CAUTION if quiet[feed] else GO) for feed in FEEDS}
    frame = " " + paint(TELEMETRY, monitor["frame"].text)
    for feeds in (  # as much as fits
        [f"{lamps['vehicle']} {paint(LEGEND, 'Vehicle')}{frame}"]
        + [
            f"{lamps[f]} {paint(LEGEND, f.capitalize())}" for f in ("ground", "weather")
        ],
        [f"{lamps[f]} {paint(LEGEND, f.capitalize())}" for f in FEEDS],
        list(lamps.values()),
    ):
        right = "   ".join(feeds)
        if width_of(title) + 1 + width_of(right) <= width:
            return spread(title, right, width)
    return fit(title, width)


def sign(monitor: Monitor, quiet: bool = False) -> str:
    text = "T+" if monitor["clock.counting_up"].value else "T−"
    return paint(CLOCK_QUIET if quiet else CLOCK, text, bold=True)


def pad_lamps(monitor: Monitor) -> str:
    items = [
        ("loaded", "Propellant loaded"),
        ("strongback", "Strongback retracted"),
        ("deluge", "Water deluge"),
    ]
    return "   ".join(
        lamp(bool(monitor[f"pad.{id}"].value))
        + " "
        + paint(TEXT if monitor[f"pad.{id}"].value else LEGEND, label)
        for id, label in items
    )


def trajectory(monitor: Monitor, width: int, chart_rows: int) -> list[str]:
    lines = []
    for id, label, units in [
        ("altitude", "Altitude", "km"),
        ("velocity", "Velocity", "m/s"),
    ]:
        lines.append(spread(paint(LEGEND, label), reading(monitor, id, units), width))
        lines.extend(chart(monitor[id].history, width, chart_rows))
    lines.append(
        paint(LEGEND, "Downrange ")
        + reading(monitor, "downrange", "km")
        + paint(LEGEND, "    Stage ")
        + paint(TELEMETRY, monitor["stage"].text, bold=True)
    )
    return lines


SHORT_GAUGES = {"q": "Dyn pressure"}
GAUGES = [  # id, label, units, full scale (None: the element's own ratio)
    ("q", "Dynamic pressure", "kPa", None),
    ("acceleration", "Acceleration", "g", None),
    ("throttle", "Throttle", "%", None),
    ("pitch", "Pitch", "°", 90.0),
]


def dynamics(monitor: Monitor, width: int, *, compact: bool = False) -> list[str]:
    """Each gauge a bar under its reading (one line each when compact)."""
    lines = []
    for id, label, units, scale in GAUGES:
        ratio = (
            monitor[id].ratio if scale is None else (number(monitor, id) or 0) / scale
        )
        value = reading(monitor, id, units)
        if compact:
            label = SHORT_GAUGES.get(id, label)
            shown = " " * max(10 - width_of(value), 0) + value
            bar = gauge(ratio, max(width - 24, 3))
            lines.append(fit(paint(LEGEND, label), 13) + bar + " " + shown)
            continue
        if lines:
            lines.append("")
        lines += [spread(paint(LEGEND, label), value, width), gauge(ratio, width)]
    return lines


def propulsion(monitor: Monitor, tank_rows: int) -> list[str]:
    on = monitor["engines"].as_dict()
    ring = {i: e for i, e in zip([0, 4, 2, 6, 1, 5, 3, 7], ENGINES[1:9], strict=True)}
    e = {i: lamp(on[ring[i]]) for i in ring}
    center = lamp(on[ENGINES[0]])
    octaweb = [
        f" {e[7]}  {e[0]}  {e[1]}",
        f"{e[6]}   {center}   {e[2]}",
        f" {e[5]}  {e[4]}  {e[3]}",
    ]
    vacuum = ["", f"   {lamp(on['mvac'])}", ""]

    def tanks(stage: str) -> list[str]:
        both = [monitor[f"propellant.{stage}_{fluid}"] for fluid in ("lox", "fuel")]
        columns = [
            [
                *(f" {row}" for row in tank(bar.ratio, tank_rows)),
                paint(LEGEND, label),
                paint(TELEMETRY, f"{round(bar.ratio * 100)}%", bold=True),
            ]
            for bar, label in zip(both, ["LOX", "RP-1"], strict=True)
        ]
        return side_by_side(columns, [6, 6], "")

    left = [paint(LEGEND, "Stage 1"), *octaweb, "", *tanks("s1")]
    right = [paint(LEGEND, "Stage 2"), *vacuum, "", *tanks("s2")]
    return side_by_side([left, right], [15, 11], " ")


def poll(monitor: Monitor, width: int) -> list[str]:
    lines = []
    for name, label in CONSOLES.items():
        go = bool(monitor[f"poll.{name}"].value)
        answer = paint(GO, "Go") if go else paint(LEGEND, "Standing by")
        lines.append(spread(lamp(go) + " " + paint(TEXT, label), answer, width))
    return lines


def weather(monitor: Monitor, width: int) -> list[str]:
    direction = number(monitor, "weather.direction")
    facts = [
        paint(LEGEND, "Wind"),
        reading(monitor, "weather.wind", "kt")
        + paint(LEGEND, " from ")
        + reading(monitor, "weather.direction", "°"),
        paint(LEGEND, "Gusts ") + reading(monitor, "weather.gust", "kt"),
        paint(LEGEND, "Temperature ") + reading(monitor, "weather.temperature", "°C"),
    ]
    top = side_by_side([compass(direction), facts], [11, width - 11], "")
    return [*top, "", *chart(monitor["weather.wind"].history, width, 2)]


def events(monitor: Monitor, width: int, count: int) -> list[str]:
    entries = list(monitor["log"].entries)[-count:]
    lines = []
    for entry in entries:
        stamp, _, message = entry.partition("  ")
        room = width - len(stamp) - 2
        if len(message) > room:
            message = message[: room - 1] + "…"
        lines.append(paint(LEGEND, stamp) + "  " + paint(TEXT, message))
    return lines


def timeline(monitor: Monitor, *, short: bool) -> list[str]:
    done = monitor["events"].as_dict()
    names = SHORT_EVENTS if short else EVENT_NAMES
    return [
        lamp(done[e]) + " " + paint(TEXT if done[e] else LEGEND, names[e])
        for e in EVENTS
    ]


# -- the screens ---------------------------------------------------------------------


def screen(monitor: Monitor, width: int, height: int, web: str = "") -> str:
    """The whole frame for a ``width`` x ``height`` terminal."""
    if width < 60 or height < 20:
        return "\n".join(
            [
                paint(CAUTION, "Make the terminal at least 60 x 20"),
                paint(LEGEND, f"(it is {width} x {height})"),
            ]
        )
    usable = width - 1  # the last column is left alone, as terminals like
    full = usable >= 100 and height >= 34
    lines = (full_screen if full else compact_screen)(monitor, usable, height, web)
    return "\n".join(lines[:height])


def rule(width: int, joints: dict[int, str] | None = None) -> str:
    """A horizontal line, joined to the vertical ones at ``joints``."""
    line = ["─"] * width
    for x, mark in (joints or {}).items():
        line[x] = mark
    return paint(RULE, "".join(line))


def full_screen(monitor: Monitor, width: int, height: int, web: str) -> list[str]:
    quiet = quiet_feeds(monitor)

    def note(feed: str) -> str | None:  # "No data from ..." when a feed stops
        return FEEDS[feed][2] if quiet[feed] else None

    widths = [int((width - 2) * 0.32), int((width - 2) * 0.36)]
    widths.append(width - 2 - sum(widths))
    inner = [w - 2 for w in widths]
    first, second = widths[0], widths[0] + widths[1] + 1  # the vertical lines
    bar = paint(RULE, "│")

    def row_of(blocks: list[list[str]]) -> list[str]:
        return side_by_side(
            [[" " + line for line in block] for block in blocks], widths, bar
        )

    lines = [masthead(monitor, quiet, width), rule(width, {second: "┬"})]

    # The countdown: clock, status and pad from the ground; the flight's events.
    clock = segment_clock(
        monitor["clock.time"].text or "00:00:00", quiet=quiet["ground"]
    )
    status, pad = paint(BRIGHT, monitor["status"].text, bold=True), pad_lamps(monitor)
    if quiet["ground"]:  # all of it is the ground's: dim it, and say why
        status, pad = (
            paint(CAUTION, "No data from ground systems"),
            paint(OFF, plain(pad)),
        )
    countdown = [
        *[
            (sign(monitor, quiet["ground"]) if i == 0 else "  ") + "  " + row
            for i, row in enumerate(clock)
        ],
        "",
        status,
        pad,
    ]
    left_width = second - 1
    flight = timeline(monitor, short=False)
    if quiet["vehicle"]:
        notice = paint(CAUTION, "No data from the vehicle")
        flight = [notice, *(paint(OFF, plain(line)) for line in flight)]
    lines += side_by_side(
        [countdown, [" " + line for line in flight]], [left_width + 1, widths[2]], bar
    )
    lines.append(rule(width, {first: "┬", second: "┼"}))

    # The vehicle.
    lines += row_of(
        [
            panel(
                "Trajectory",
                trajectory(monitor, inner[0], 4),
                inner[0],
                note("vehicle"),
            ),
            panel(
                "Flight dynamics",
                dynamics(monitor, inner[1]),
                inner[1],
                note("vehicle"),
            ),
            panel("Propulsion", propulsion(monitor, 4), inner[2], note("vehicle")),
        ]
    )
    lines.append(rule(width, {first: "┼", second: "┼"}))

    # The ground and the range; the log fills what's left.
    rows = height - len(lines) - 2
    bottom = row_of(
        [
            panel("Go/no-go poll", poll(monitor, inner[0]), inner[0], note("ground")),
            panel(
                "Weather at the pad",
                weather(monitor, inner[1]),
                inner[1],
                note("weather"),
            ),
            panel("Events", events(monitor, inner[2], rows - 1), inner[2]),
        ]
    )
    lines += (bottom + [fit("", first) + bar + fit("", widths[1]) + bar] * rows)[:rows]
    lines.append(rule(width, {first: "┴", second: "┴"}))
    lines.append(footer(web, width))
    return lines


def compact_screen(monitor: Monitor, width: int, height: int, web: str) -> list[str]:
    """Everything in brief, for terminals down to 80 x 24 (or 60 x 20)."""
    quiet = quiet_feeds(monitor)
    lines = [masthead(monitor, quiet, width), rule(width)]

    time = monitor["clock.time"].text or "00:00:00"
    status, color = monitor["status"].text, BRIGHT
    if quiet["ground"]:
        status, color = "No data from ground systems", CAUTION
    if width >= 78:  # the segment clock, the status beside it
        clock = [
            (sign(monitor, quiet["ground"]) if i == 0 else "  ") + "  " + row
            for i, row in enumerate(segment_clock(time, quiet=quiet["ground"]))
        ]
        room = width - 60
        lines += side_by_side(
            [clock, [paint(color, line, bold=True) for line in wrap(status, room)]],
            [58, room],
            "  ",
        )
    else:  # too narrow for the segment clock: its reading, in amber
        digits = CLOCK_QUIET if quiet["ground"] else CLOCK
        lines.append(
            sign(monitor, quiet["ground"])
            + " "
            + paint(digits, time, bold=True)
            + "   "
            + paint(color, status, bold=True)
        )
    lines.append("  ".join(timeline(monitor, short=True)))
    lines.append(rule(width))

    half = (width - 3) // 2
    right = width - half - 3
    on = monitor["engines"].as_dict()
    tanks = [monitor[f"propellant.{stage}_lox"].ratio for stage in ("s1", "s2")]
    wind = paint(LEGEND, "  Wind ") + reading(monitor, "weather.wind", "kt")
    gusts = paint(LEGEND, ", gusts ") + reading(monitor, "weather.gust", "")
    polls = paint(LEGEND, "Poll ") + "".join(
        lamp(bool(monitor[f"poll.{n}"].value)) for n in POLL
    )
    left = trajectory(monitor, half, 2)
    right_lines = [
        *dynamics(monitor, right, compact=True),
        paint(LEGEND, "Engines ")
        + "".join(lamp(on[e]) for e in ENGINES[:9])
        + " "
        + lamp(on["mvac"]),
        paint(LEGEND, "Tanks ")
        + paint(TELEMETRY, "  ".join(f"{round((r or 0) * 100)}%" for r in tanks)),
        polls + wind + (gusts if width_of(polls + wind + gusts) <= right else ""),
    ]
    lines += side_by_side(
        [left, right_lines], [half, right], " " + paint(RULE, "│") + " "
    )
    lines.append(rule(width))
    rows = max(height - len(lines) - 1, 1)
    lines += (events(monitor, width, rows) + [""] * rows)[:rows]
    lines.append(footer(web, width))
    return lines


def wrap(text: str, width: int) -> list[str]:
    """Words onto lines of at most ``width`` characters."""
    lines: list[str] = []
    for word in text.split():
        if lines and len(lines[-1]) + 1 + len(word) <= width:
            lines[-1] += " " + word
        else:
            lines.append(word[:width])
    return lines


def footer(web: str, width: int) -> str:
    left = paint(LEGEND, "In a browser too: ") + paint(TEXT, web) if web else ""
    return spread(left, paint(LEGEND, "Ctrl+C quits"), width)
