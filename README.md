# Remote App Monitor

Live dashboards for a running program, a microcontroller, or anything that
can make an HTTP request. Send values by name and they appear in the browser
(or the terminal) as they change.

![Robot control dashboard](docs/robot-dashboard.jpg)

- **One line to start.** `start()` serves a dashboard in the background and
  prints its address; `monitor.set("progress", 5)` works from any thread, and
  elements appear the first time you use them.
- **No code needed.** Pipe a program into `app-monitor`, `curl` values to it,
  or point it at a serial port or ZeroMQ socket.
- **Any language.** `POST /update` with JSON or `key=value` lines.
- **Keeps up with fast devices.** Serial and ZeroMQ are read on background
  threads and drained completely, so the display never falls behind and a
  stalled device can't freeze it.
- **Honest about staleness.** Pages reconnect by themselves, show how old the
  data is, and dim values while disconnected or (optionally) when a source
  goes quiet.
- **Plain HTML dashboards.** Mark any element `data-bind="<id>"` and it stays
  live, like the segment-display panel above. Otherwise a clean generic page
  is generated.

## Install

Requires Python 3.11+.

```bash
pip install "remote-app-monitor[web]"        # add serial, zmq, or use [all]
```

Until the first PyPI release, install from GitHub:

```bash
pip install "remote-app-monitor[web] @ git+https://github.com/davidson-engineering/remote-app-monitor"
```

Try it: `app-monitor --demo`, then open the address it prints.

## Quick start

### From inside a Python program

```python
import time

from app_monitor import start

monitor = start()  # prints "Dashboard: http://127.0.0.1:8080/"

for i in range(1, 101):
    time.sleep(0.1)  # your work
    monitor.set("progress", i)
    monitor.set("status", "running" if i < 100 else "done")
```

`set()` returns immediately and is safe from any thread. Each new id becomes
an element (a lamp for `True`/`False`, text otherwise; ids like `"job.rate"`
are grouped under `job`). Long floats are shortened to 6 significant digits.
The dashboard stops when your program exits, or call `monitor.stop()`.

### Without writing code

```bash
# Pipe a program: lines like "progress=5 status=running" or JSON objects
# become values; everything else is printed as usual.
my_program | app-monitor

# Push values from anything: shell scripts, cron jobs, other languages.
app-monitor &
curl -d 'temperature=21.5 status=ok' http://127.0.0.1:8080/update
curl -d '{"job": {"progress": 5}}' http://127.0.0.1:8080/update

# Read a device.
app-monitor --serial auto --csv temperature,humidity
app-monitor --zmq tcp://localhost:5556
```

`app-monitor --help` lists every option, including `--terminal` to draw in the
terminal instead of the browser.

### From a separate Python program

The client uses only the standard library, so the program sending values
doesn't need the dashboard's dependencies:

```python
from app_monitor import Client

dashboard = Client()  # http://127.0.0.1:8080
dashboard.set("progress", 5)
dashboard.update({"rate": 18.6, "status": "running"})
```

Updates are sent in order in the background. If the dashboard isn't running
yet they're kept and retried, and anything pending is sent when your program
exits, so even a two-line script's values arrive. For one message,
`from app_monitor.client import send` and `send({"status": "done"})`.

## Choosing how values look

Declare elements for bars, charts, units and number formats. Anything you
don't declare still appears as text.

```python
from app_monitor import Monitor, ProgressBar, Sparkline, TextFormat

monitor = Monitor()
monitor.add(
    ProgressBar("progress", total=500, label="Progress"),
    Sparkline("rate", label="Items/s", format=TextFormat(precision=1)),
)
monitor.start()
```

| Element | Shows | Update with |
| --- | --- | --- |
| `TextElement` | a value, optionally scaled and number-formatted | any value |
| `Sparkline` | a number and a chart of its recent history | a number |
| `ProgressBar` | a filling bar from 0 to `total` | a number |
| `RangeBar` | a marker on a min to max track | a number |
| `IndicatorLamp` | on/off | `True`/`False`, `1`/`0`, `on`/`off` |
| `MachineState` | named on/off states packed into one integer | an integer (bit 0 = first state) |
| `Coordinate` | a multi-axis position | a list, or per axis: `"<id>.x"` |
| `Table` | a grid of cells | per cell: `"<id>.<row>.<column>"` |
| `LogMonitor` | the latest messages | a string |

Multi-part elements take updates to a field, addressed as `"<id>.<field>"`.
`monitor.add_group("X", [...])` adds copies of elements under a heading with
ids `X.<id>`, so one list can serve several axes.

`TextFormat(width, precision, force_sign, padding)` formats numbers:
`TextFormat(width=10, precision=3, force_sign=True, padding="0")` turns `27.31`
into `+00027.310`. `Style(fg, bg, bold, dim)` adds colour in the terminal.

For a fixed, curated dashboard use `Monitor(strict=True)`: unknown ids are
then rejected and logged (once per id) instead of creating elements.
`max_elements` (default 500) stops a noisy link creating elements without end.

## Running

| Call | Runs |
| --- | --- |
| `monitor.start(sources=..., outputs=...)` | in a background thread; returns once the dashboard is up (setup errors such as a busy port are raised here) |
| `monitor.serve(sources=..., outputs=...)` | in the foreground until Ctrl+C, then returns quietly |
| `await monitor.run(sources=..., outputs=...)` | inside your own asyncio program |

`outputs` defaults to a `WebDashboard()` for `start()` and `serve()`. The
monitor can also be used as a context manager: `with monitor.start(): ...`.

## Devices and other sources

```python
from app_monitor import CsvDecoder, Monitor, SerialSource

monitor = Monitor()
# The device prints one line per sample: "21.5,48,OK"
device = SerialSource(
    "auto", 115200, decoder=CsvDecoder(["temperature", "humidity", "status"])
)
monitor.serve(sources=[device])
```

| Source | Reads |
| --- | --- |
| `SerialSource(port, baudrate, decoder=...)` | a serial port (`"auto"` picks the first USB device); waits for it and reconnects |
| `ZmqSource(endpoint, pattern="sub")` | ZeroMQ PUB/SUB (a publisher that binds `endpoint`) |
| `ZmqSource(endpoint, pattern="pull")` | ZeroMQ PUSH/PULL: binds `endpoint`; unlike PUB/SUB, nothing sent before the monitor starts is lost |
| `StdinSource()` | piped standard input, as `app-monitor` does |
| `SimulatedSource(fn, rate)` | `fn(seconds)` called `rate` times a second, for demos and tests |

| Decoder | Wire format |
| --- | --- |
| `KeyValueDecoder()` | `X.velocity 1.5`, or `key=value` pairs: `progress=5 status="two words"` |
| `CsvDecoder(keys)` | `1.5,-2.0,OK`, values in the order of `keys` |
| `JsonDecoder()` | `{"X": {"velocity": 1.5}}` per line; nesting addresses groups and fields |
| `BinaryFrameDecoder(names, scale=1000)` | the binary frame protocol below |

#### Binary frame protocol

For firmware where text is too slow or too large:

```
0xAA | id | type | value | n | param_1 ... param_n | 0xFF
```

- `type 0x01`: `value` is a little-endian int32, divided by `scale` (1000 by default).
- `type 0x02`: `value` is a NUL-terminated UTF-8 string (up to 255 bytes).
- `id` and the params are single bytes, translated through `names`
  (`{1: "X", 10: "velocity"}`). The update key is the id's name followed by the
  params' names, so id `X` with param `velocity` updates `"X.velocity"`.

Corrupt frames are skipped and decoding resumes at the next `0xAA`.

## Outputs

**`WebDashboard(page=None, static_dir=None, host="127.0.0.1", port=8080, title="Monitor", token=None, stale_after=None)`**
serves `page` (or the generic page), streams values over a WebSocket and
accepts `POST /update`. `stale_after=2` dims values when no data has arrived
for 2 seconds, for sources that should update continuously. Your own page
loads `/_app_monitor/app_monitor.js` and marks up elements:

```html
<link rel="stylesheet" href="/_app_monitor/panel.css">

<span data-bind="temperature"></span>                                  <!-- text -->
<div class="led" data-bind="machine.estop" data-mode="state"></div>    <!-- data-state="on"/"off" -->
<div class="bar" data-bind="X.velocity.ratio" data-mode="width"></div> <!-- width: 0-100% -->
<div data-bind="rate.values" data-mode="sparkline"></div>              <!-- line chart -->

<!-- A segment display: data-ghost draws the unlit segments. -->
<div class="lcd">
  <span class="lcd-field" data-ghost="~~~~~~.~~~"><span data-bind="position_x"></span></span>
</div>

<script src="/_app_monitor/app_monitor.js"></script>
```

Object values are flattened, so a `RangeBar` binds as `<id>.text` and
`<id>.ratio`, a `Sparkline` as `<id>.text` and `<id>.values`, and a
`MachineState` as `<id>.<state>`. `panel.css` provides the segment display
(`.lcd`, `.lcd-field`) and LED (`.led`, `.led-red`, `.led-amber`) styles.
Files in `static_dir` are served under `/static/`.

**`TerminalDisplay(width=60, fps=30)`** draws a full-screen view in the
terminal's alternate screen. Log to a file while it runs; anything printed to
the terminal gets drawn over.

### Network access and security

The dashboard listens on `127.0.0.1` by default, so only the same machine can
see it. With `host="0.0.0.0"` (or `--host 0.0.0.0`) anyone who can reach the
port can view the values and, unless you set `token`, post updates. Viewing
has no authentication and traffic is not encrypted: on untrusted networks,
reach the dashboard through an SSH tunnel or a reverse proxy with TLS and
authentication.

## Examples

These live in the repository (`git clone` it, then `pip install -e ".[all]"`):

```bash
python examples/robot_dashboard/dashboard.py --simulate    # the panel above
python examples/zmq_publisher.py &
python examples/terminal_axes.py --zmq tcp://localhost:5556
```

No hardware? `examples/fake_device.py` creates a virtual serial port (macOS
and Linux) and streams to it, so you can exercise the real serial path:

```bash
python examples/fake_device.py              # prints e.g. /dev/ttys012
python examples/robot_dashboard/dashboard.py --port /dev/ttys012
```

## Extending

- **Element:** subclass `Element` and implement `update(value)`,
  `to_json()` (what the browser receives) and `render(width)` (terminal text).
  Override `update_field` to accept `"<id>.<field>"` updates.
- **Wire format:** subclass `LineDecoder` and implement
  `decode_line(line) -> {id: value}`, raising `DecodeError` for bad input; or
  subclass `Decoder` for binary formats.
- **Source:** any object with an async generator method `updates()` that
  yields lists of `{id: value}` mappings.
- **Output:** any object with `async def run(monitor)` (and optionally
  `async def start(monitor)` for setup that can fail). Use
  `await monitor.wait_for_change(version)` and
  `monitor.changes_since(version)` to react to new data.

See [CONTRIBUTING.md](CONTRIBUTING.md) for development and releases.

## License

MIT. The bundled DSEG fonts are © keshikan, licensed under the SIL Open Font
License 1.1 (`src/app_monitor/web/static/fonts/DSEG-LICENSE.txt`).
