# CLAUDE.md

`remote-app-monitor` (import `app_monitor`): live web and terminal dashboards
fed by programs, devices and HTTP. The guide below is how to *use* it to build
a monitor; it ships with the package (`app-monitor --guide`), so agents in
other projects get the same instructions.

@src/app_monitor/GUIDE.md

---

# Working on this repository

## Commands

```bash
uv sync --all-extras                    # venv with every extra and dev tool
uv run playwright install chromium      # once: the browser tests need it
uv run pytest                           # ~210 tests, ~15 s (-m "not browser" skips Chromium)
uv run ruff check . && uv run ruff format --check .
uv run app-monitor --demo               # see the generic page with every element type
uv run --all-extras examples/launch_control/launch_control.py    # the showcase (README screenshot)
uv run python examples/robot_dashboard/dashboard.py --simulate   # the segment-display panel
uv run --all-extras python docs/screenshot.py   # retake the README screenshot (~80 s)
```

## Layout

| Path | What |
| --- | --- |
| `src/app_monitor/monitor.py` | `Monitor`: element registry, id routing, auto-created elements, change versions, thread-safe updates, `start`/`serve`/`run`/`stop` |
| `src/app_monitor/elements.py` | element types; each has `update`, `to_json` (browser) and `render(width)` (terminal) |
| `src/app_monitor/decoders.py` | bytes to `{id: value}`: CSV, key/value + logfmt pairs, JSON, binary frames |
| `src/app_monitor/sources/` | serial, ZeroMQ, stdin, simulated; `_thread.py` runs blocking I/O off the event loop |
| `src/app_monitor/web/` | aiohttp server (`/`, `/ws`, `POST /update`, `GET /values`) and `static/`: `app_monitor.js` (data-bind client: text, `state`, `width`, `sparkline`, `var`, `data-stale-after`), `panel.css` (LCD/LED styles), `auto.html` (generic page), fonts |
| `src/app_monitor/terminal.py` | full-screen terminal output |
| `src/app_monitor/client.py` | stdlib-only HTTP client for other processes |
| `src/app_monitor/cli.py`, `demo.py` | the `app-monitor` command and `--demo` data |
| `src/app_monitor/GUIDE.md` | the agent guide (imported above, printed by `--guide`) |
| `examples/` | the gallery (`examples/README.md`): `launch_control/` (the showcase: custom page, three feeders), robot dashboard (segment displays, serial), numbered single-file examples, terminal, ZeroMQ publisher, Docker, `fake_device.py` (virtual serial port) |
| `docs/` | README images; `screenshot.py` retakes the launch control one |

## Invariants

- **Never block the event loop.** Blocking I/O (serial, ZeroMQ, stdin) runs on
  a thread through `sources/_thread.thread_items`; the loop only decodes and
  applies.
- **Thread safety lives in `Monitor`.** Public mutators called off the loop
  thread are handed to it (`_loop_elsewhere`, `_on_loop`); internal state is
  only touched on the loop thread. Keep new mutators on that path.
- **Bad input never raises out of a source.** Decoders reject with
  rate-limited logging (`Decoder._reject`); the monitor rejects per id,
  logged once (`Monitor._reject`).
- **Optional dependencies stay optional.** Core modules import only the
  standard library; `SerialSource`, `ZmqSource` and `WebDashboard` are loaded
  lazily in `__init__.py`; `client.py` must not import aiohttp, zmq or serial
  (a test checks).
- **Be visible.** The dashboard prints its address; the package never
  installs a `NullHandler`; setup errors are one clear line, not a traceback.
- **Pages are HTML.** Display logic is `data-bind` / `data-mode` in
  `app_monitor.js`; no per-page JavaScript. New element kinds need a
  `to_json` shape the generic page (`auto.html`) knows how to draw.

## Tests

- Prefer end to end: pseudo-terminal serial ports (`tests/conftest.py`),
  real ZeroMQ sockets, real WebSockets (`aiohttp.test_utils`), the CLI as a
  subprocess, and a real browser for `app_monitor.js` (`tests/test_browser.py`,
  marked `browser`). Use `port=0` and `announce=False` for dashboards in tests.
- Browser tests use Playwright's async API. Not pytest-playwright: its sync
  fixtures run their own event loop and break every pytest-asyncio test that
  runs after them.
- `filterwarnings = error`: close every socket, pipe, file and HTTP error
  response, or the test fails.
- Serial tests need pseudo-terminals and skip on Windows. CI runs Linux,
  macOS and Windows on Python 3.11 to 3.14; check behaviour that differs
  between versions (e.g. `subprocess`, `asyncio`) on 3.11 too.
- A bug fix starts with a test that fails without it.

## Before finishing a change

- Run the tests and ruff.
- UI changes: look at the generic page (`app-monitor --demo`), the launch
  control and the robot examples in a browser, at desktop and 390 px widths.
  Changes to the launch control page: retake the README screenshot.
- API or docs changes: run every snippet in `README.md` and `GUIDE.md` as
  written, in a clean virtualenv; they are user-facing contracts.
- Releases: see `CONTRIBUTING.md` (tag `v<__version__>`, trusted publishing).
