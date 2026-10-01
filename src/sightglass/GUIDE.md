# Building monitors with sightglass: a guide for AI agents

`sightglass` shows live values from a program, a device, or anything that
can make an HTTP request, in a browser (default) or the terminal. Values are
sent by id (`"progress"`, `"X.velocity"`); the first value sent for an id
creates its display. Print this guide from any project with
`sightglass --guide`; full reference: `sightglass --help` and the README.

## 1. Install

```bash
pip install "sightglass[web]"     # add serial / zmq / ads extras as needed, or [all]
# until it is on PyPI:
pip install "sightglass[web] @ git+https://github.com/davidson-engineering/sightglass"
```

Python 3.11+. The package has no required dependencies; `[web]` adds
aiohttp for the browser dashboard, `[serial]` pyserial, `[zmq]` pyzmq,
`[ads]` pyads (Beckhoff PLCs).

## 2. Pick the integration

| Situation | Do this |
| --- | --- |
| You can edit the Python program | `monitor = start()`, then `monitor.set(id, value)` anywhere |
| Another language, a shell script, or a program you can't edit | run `sightglass`, then `POST /update` (curl) |
| The program can print lines | print `key=value` pairs; run `program \| sightglass` |
| A separate Python process should report to a shared dashboard | `Client().set(id, value)` (standard library only) |
| A microcontroller on a serial port | `sightglass --serial auto --csv a,b,c` (or `--json`, `--binary`) |
| A ZeroMQ publisher | `sightglass --zmq ENDPOINT` (`--pull` for PUSH senders: nothing lost) |
| A Beckhoff TwinCAT PLC (ADS) | `sightglass --ads NETID --vars 'MAIN.*'`, or an interface file: `sightglass --ads plc.toml` |
| An asyncio program | `await monitor.run(outputs=[WebDashboard()])` in a task |
| Just show the user what it looks like | `sightglass --demo --open` (every display), `sightglass --demo launch --open` (a hand-built page) |

Choose `start()` when the dashboard should live and die with the program.
Choose a separate `sightglass` process (fed by `Client`, curl or a pipe) when
several programs report to one dashboard, or it should outlive them.

## 3. Recipes

### Inside a Python program

```python
import time

from sightglass import start

monitor = start()  # prints "Dashboard: http://127.0.0.1:8080/", returns at once
monitor.set("status", "loading")
for i in range(1, 51):
    time.sleep(0.1)  # the program's real work
    monitor.set("progress", i)
    monitor.set("rate", i / (0.1 * i))
monitor.set("status", "done")
```

- `set()` never blocks and is safe from any thread.
- `start()` raises `OSError` if the port is taken; pass
  `outputs=[WebDashboard(port=0)]` for any free port (the address is printed,
  and is `web.url` on the `WebDashboard` object).
- When the program exits, open pages keep the final values, marked
  disconnected.

### Choosing how values look

Declare elements for bars, charts, lamps, units and number formats. Anything
not declared still appears, as text.

```python
from sightglass import (
    IndicatorLamp,
    Monitor,
    ProgressBar,
    RangeBar,
    Sparkline,
    TextFormat,
    WebDashboard,
)

monitor = Monitor()
monitor.add(
    ProgressBar("progress", total=50, label="Progress"),
    Sparkline("rate", label="Items/s", format=TextFormat(precision=1)),
    IndicatorLamp("healthy", label="Healthy"),
)
monitor.add_group(
    "X", [RangeBar("velocity", min_value=-10, max_value=10, units="mm/s")]
)
monitor.start(outputs=[WebDashboard(title="Batch job")])
monitor.set("healthy", True)
monitor.set("X.velocity", 2.5)
```

### From any language

```bash
sightglass --title "Build farm" &
curl -d 'progress=5 status=running' http://127.0.0.1:8080/update
curl -d '{"job": {"progress": 5, "ok": true}}' http://127.0.0.1:8080/update
curl http://127.0.0.1:8080/values      # read back what is shown, as JSON
```

The body is a JSON object (or a list of objects), or lines of `key=value`
pairs / `id value`. Nested JSON objects address groups: `{"job": {"progress":
5}}` sets `job.progress`. `204` means accepted; `400` says what was wrong;
`401` means the dashboard has a `--token` and the request didn't send
`Authorization: Bearer <token>`.

### Piping a program's output

```bash
my_program | sightglass
```

A line made entirely of `key=value` pairs (`progress=5 status="two words"`) or
a JSON object becomes values; every other line is printed through unchanged.
In Python: `print(f"progress={i} rate={rate:.1f}", flush=True)`.

### From a separate Python process

```python
from sightglass import Client

dashboard = Client("http://127.0.0.1:8080")  # token="..." if the dashboard has one
dashboard.set("progress", 5)
dashboard.update({"status": "running", "errors": 0})
```

Updates are sent in order in the background, retried until the dashboard is
reachable, and flushed when the program exits. `from sightglass.client import
send; send({"status": "done"})` sends one message immediately and raises
`ConnectionError` if it can't.

### A serial device

```bash
sightglass --serial auto --csv temperature,humidity   # device prints "21.5,48\n"
sightglass --serial /dev/ttyUSB0 --json               # {"temperature": 21.5}
sightglass --serial auto --binary 1=X,2=Y,10=velocity # binary frames, see README
```

`auto` picks the first USB serial device. The source waits for the device and
reconnects after it is unplugged. In Python:

```python
from sightglass import CsvDecoder, Monitor, SerialSource

monitor = Monitor()
device = SerialSource("auto", 115200, decoder=CsvDecoder(["temperature", "humidity"]))
monitor.serve(sources=[device])  # runs until Ctrl+C
```

### A Beckhoff TwinCAT PLC (ADS)

```bash
sightglass --ads 5.12.34.56.1.1 --vars 'MAIN.*,GVL.fTemperature'   # :851 by default
sightglass --ads plc.toml                                          # an interface file
```

Ids are the PLC's variable names; structs and arrays give one id per member
(`MAIN.stAxis.fPosition`, `GVL.aTemps[1]`). `*` in a name matches top-level
variables only; name struct members exactly, or declare the struct. An
interface file names the PLC, the variables and their types:

```toml
target = "5.12.34.56.1.1:851"
types = """
TYPE ST_Axis :
STRUCT
    fPosition : LREAL;
    bEnabled  : BOOL;
END_STRUCT
END_TYPE
"""

[variables]
"MAIN.stAxis1" = "ST_Axis"
"GVL.fTemperature" = {}
"GVL.nSetpoint" = { write = true }
```

`types` is Structured Text pasted from the PLC project (structs, enums,
aliases, `pack_mode` attributes). In Python: `AdsSource("5.12.34.56.1.1",
["MAIN.*"])` or `AdsSource.from_file("plc.toml")`, passed in `sources=[...]`.

- **Writes are off unless all four hold:** `write = true` on the variable,
  `--allow-writes` (`allow_writes=True`), `SIGHTGLASS_ALLOW_WRITES=1` in the
  environment, and `--token` on the dashboard (writes over HTTP need it). Then
  `curl -H 'Authorization: Bearer TOKEN' -d 'GVL.nSetpoint=75' .../update`
  writes; 204 means the PLC took it, 403/400/409 say why not. Never enable
  writes unless the user asked for them.
- **Linux and macOS:** the PLC needs a route to this machine. If it doesn't
  answer, the warning names the AMS net id and IP address to add.
- **Windows:** needs TwinCAT's ADS router installed (XAE, XAR or TC1000).
- Warnings name every variable that couldn't be read and why (unknown name,
  undeclared struct type, size mismatch with the declaration); read them.

## 4. Ids and values

- An id is any string. Dots group: `job.rate` is shown as `rate` under a `job`
  heading. Ids are case-sensitive.
- A new id creates an element: a lamp for `True`/`False`, text otherwise.
  Declare the element first to get a bar, chart, units or format.
- curl, serial and pipes send text, so `pump=on` shows the text `on`. Declare
  `IndicatorLamp("pump")` to show a lamp; it accepts `on`/`off`, `1`/`0`,
  `true`/`false`.
- Multi-part elements take fields: `table.row.column`, `machine.estop`,
  `position.x`.
- Floats that need more than 6 significant digits are shortened; use
  `TextFormat(precision=2)` for a fixed number of decimals.
- `Monitor(strict=True)` rejects ids that weren't declared (logged once per
  id); by default at most `max_elements=500` are created.

| To show | Element | Update with |
| --- | --- | --- |
| a value or message | `TextElement(id, units="°C", format=TextFormat(precision=1))` | anything |
| a trend | `Sparkline(id, points=60, scale=1 / 1024, units="KB/s")` | numbers |
| a long trend of a fast value | `Sparkline(id, points=300, interval=1)`: one point a second | numbers |
| progress | `ProgressBar(id, total=100)` | a number |
| a reading within limits | `RangeBar(id, min_value=0, max_value=100, units="mm")` | a number |
| on/off | `IndicatorLamp(id)` | bool, 1/0, on/off |
| many flags in one integer | `MachineState(id, states=[...])` (bit 0 first) | an integer |
| a position | `Coordinate(id, axes=("x", "y", "z"))` | a list, or `id.x` |
| a grid | `Table(id, rows=[...], columns=[...])` | `id.row.column` |
| recent events | `LogMonitor(id, lines=10, timestamp=True)` | a string |

## 5. A custom page

For a branded or hardware-style panel, write plain HTML and bind elements by
id; no JavaScript is needed. `sightglass --demo launch` runs a complete
example (gauges, tanks, lamps, several feeds) whose source ships with the
package: `python -c "import sightglass.launch as m; print(m.HERE)"` prints
where. The repository's `examples/robot_dashboard/` is another (segment
displays, LEDs), and its `examples/README.md` has a runnable example of each
route in this guide (one command each), plus a Docker deployment.

```html
<link rel="stylesheet" href="/_sightglass/panel.css">
<span data-bind="temperature"></span>
<div class="led" data-bind="pump" data-mode="state"></div>
<div data-bind="rate.values" data-mode="sparkline" style="height: 24px"></div>
<div class="needle" data-bind="pressure.ratio" data-mode="var"></div>
<span data-bind="wind" data-stale-after="3"></span>
<script src="/_sightglass/sightglass.js"></script>
```

Serve it with `WebDashboard("page.html", static_dir="static")` (files in
`static_dir` are at `/static/`). Bars bind `id.text` and `id.ratio`
(`data-mode="width"`); sparklines `id.text` and `id.values`. A
`data-mode="state"` element is off for `false`, `0`, `"off"`, `"0"`,
`"false"`, `"no"` and empty, and on for anything else.

- **Gauges without JavaScript:** `data-mode="var"` sets the CSS variable
  `--value` to the number (unset if it isn't one), e.g.
  `.needle { rotate: calc(var(--value) * 270deg - 135deg) }` for a `RangeBar`
  ratio, or `height: calc(var(--value) * 100%)` for a level.
- **Several feeds:** `data-stale-after="3"` gives an element
  `data-stale="true"` while its value hasn't arrived for 3 s (or ever), so
  CSS can show which source went quiet, e.g.
  `section:has([data-stale]) { opacity: 0.3 }`. Bind it to a value the source
  sends every time; resending the same value counts as arriving.
- **Themes:** pages come in `classic` and `terminal` (phosphor green,
  cockpit-style instruments, optional CRT effects); `--theme` /
  `WebDashboard(theme=...)` sets the default and a switch on the page lets
  viewers change it. For your own page:
  `<script src="/_sightglass/theme.js"></script>` and
  `<link rel="stylesheet" href="/_sightglass/terminal.css">` in `<head>`,
  buttons with `data-theme-switch` / `data-crt-switch`, and CSS under
  `html[data-theme="terminal"]` using the `--crt-*` colours and fonts
  (`--crt-display-font` for a big clock-like display, at multiples of 11px;
  `--crt-readout-font` for readings, at multiples of 8px). The CRT effects
  cover any such page; animate your panels with its `crt-draw` and
  `crt-type` keyframes, only under `prefers-reduced-motion: no-preference`.
- **In a terminal:** `TerminalDisplay(screen=fn)` draws your own layout:
  `fn(monitor, width, height)` returns the whole frame, reading
  `monitor[id].text` and coloured with `Style(fg=214)` (xterm's 256
  colours). `sightglass --demo launch --terminal` runs a full one, in the
  package's `sightglass/launch/screen.py`.

## 6. Deploying

- **Other machines:** `WebDashboard(host="0.0.0.0")` / `--host 0.0.0.0`.
  Anyone who can reach the port can then view the values, and post values
  unless `token` / `--token` is set. Viewing is never authenticated or
  encrypted: on untrusted networks use `ssh -L 8080:127.0.0.1:8080 host` or a
  reverse proxy with TLS and auth.
- **Continuous sources:** `stale_after=2` / `--stale-after 2` dims values
  when no data has arrived for 2 s.
- **Ports:** 8080 by default. On macOS avoid 5000 (AirPlay).
- **As a Linux service** (a dashboard that outlives the programs feeding it):

  ```ini
  # /etc/systemd/system/sightglass.service
  [Unit]
  Description=Live dashboard
  After=network-online.target

  [Service]
  ExecStart=/opt/sightglass/venv/bin/sightglass --host 0.0.0.0 --token change-me
  Restart=on-failure

  [Install]
  WantedBy=multi-user.target
  ```

## 7. Verify before saying it works

1. The program printed `Dashboard: http://...`; use that address.
2. `curl -s http://127.0.0.1:8080/values` lists the ids with their current
   values (what the page shows).
3. For pushed values, `curl -s -o /dev/null -w '%{http_code}' -d 'check=1'
   http://127.0.0.1:8080/update` prints `204`.
4. Warnings (bad values, unknown ids with `strict=True`, a missing serial
   device, PLC variables left out) go to stderr; read them.
5. Look at the page in a browser if you can: values change live and the
   status says "Live".

## 8. Gotchas

- A program that exits takes a `start()` dashboard with it. For a dashboard
  that stays up, run `sightglass` separately and send with `Client` or curl.
- `TerminalDisplay` / `--terminal` redraws the whole terminal: don't print
  while it runs (the command logs to `sightglass.log` instead).
- ZeroMQ PUB/SUB drops messages sent before the subscriber connects; use
  PUSH with `ZmqSource(endpoint, pattern="pull")` / `--zmq ... --pull`.
- In asyncio code use `await monitor.run(...)`; `start()` runs its own loop in
  a thread and `serve()` blocks.
- In tests: `WebDashboard(port=0, announce=False)`, `monitor.start(...)`,
  `web.url`, then `monitor.stop()`. `SimulatedSource(fn, rate)` generates
  data without hardware.
