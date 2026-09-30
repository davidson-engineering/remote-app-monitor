# Examples

Each example shows one way to get live values onto a dashboard, and runs with
one command. The dashboard opens in your browser; stop with Ctrl+C.

The commands use [uv](https://docs.astral.sh/uv/), which fetches everything an
example needs on the fly: no clone, no virtualenv. Install it once with
`curl -LsSf https://astral.sh/uv/install.sh | sh` (macOS, Linux) or
`powershell -c "irm https://astral.sh/uv/install.ps1 | iex"` (Windows).

Without uv: `pip install "remote-app-monitor[web] @ git+https://github.com/davidson-engineering/remote-app-monitor"`,
download the example, and run it with `python`.

## Start here: the demo

Every kind of display, with simulated data:

```bash
uvx --from "remote-app-monitor[web] @ git+https://github.com/davidson-engineering/remote-app-monitor" app-monitor --demo --open
```

## 1. Hello, dashboard

A Python program reports what it's doing with `start()` and `set()`. Nothing
is declared: each value gets a display the first time it's set.
[01_hello.py](01_hello.py)

```bash
uv run https://raw.githubusercontent.com/davidson-engineering/remote-app-monitor/main/examples/01_hello.py
```

## 2. System monitor

This computer's CPU, memory, disk, network and busiest processes. Shows how to
choose the displays: charts for trends, bars for capacity, a table, groups.
[02_system_monitor.py](02_system_monitor.py)

```bash
uv run https://raw.githubusercontent.com/davidson-engineering/remote-app-monitor/main/examples/02_system_monitor.py
```

## 3. From the shell

Any language can feed a dashboard over HTTP. This bash script starts one and
posts readings with `curl`. [03_from_the_shell.sh](03_from_the_shell.sh)

```bash
curl -fsSL https://raw.githubusercontent.com/davidson-engineering/remote-app-monitor/main/examples/03_from_the_shell.sh | bash
```

## 4. Pipe a program's output

A program that only prints: lines of `key=value` pairs become values, and its
other output still shows in the terminal. The program needs no dashboard code
at all. [04_pipe.py](04_pipe.py)

```bash
uv run https://raw.githubusercontent.com/davidson-engineering/remote-app-monitor/main/examples/04_pipe.py | uvx --from "remote-app-monitor[web] @ git+https://github.com/davidson-engineering/remote-app-monitor" app-monitor --open
```

## 5. Many processes, one dashboard

Four worker processes report their progress with `Client`, which needs only
the standard library. In a real system they could be separate programs on
separate machines. [05_many_processes.py](05_many_processes.py)

```bash
uv run https://raw.githubusercontent.com/davidson-engineering/remote-app-monitor/main/examples/05_many_processes.py
```

## 6. Deploy with Docker

The dashboard as a service, and a second container feeding it over the
network with a token. Stop the feeder (`docker compose stop feeder`) to see
the page flag the missing data after 5 seconds. [docker/](docker/)

```bash
git clone https://github.com/davidson-engineering/remote-app-monitor
cd remote-app-monitor/examples/docker
docker compose up
```

Then open <http://localhost:8080>. The port is only reachable from this
machine; see `compose.yaml` to share it, and set `TOKEN` to your own secret.

## 7. In the terminal

A three-axis machine drawn in the terminal instead of a browser.
[terminal_axes.py](terminal_axes.py)

```bash
uv run https://raw.githubusercontent.com/davidson-engineering/remote-app-monitor/main/examples/terminal_axes.py --simulate
```

`--zmq tcp://localhost:5556` reads [zmq_publisher.py](zmq_publisher.py)
instead, and `--port /dev/ttyUSB0` a device sending binary frames.

## 8. A custom panel (and a serial device)

A hand-built control panel with segment displays and LEDs, written in plain
HTML with `data-bind` attributes: the screenshot in the main README. It reads
a microcontroller over serial, or simulated data.
[robot_dashboard/](robot_dashboard/)

```bash
git clone https://github.com/davidson-engineering/remote-app-monitor
cd remote-app-monitor
uv run --all-extras examples/robot_dashboard/dashboard.py --simulate
```

No microcontroller? On macOS or Linux, [fake_device.py](fake_device.py) creates
a virtual serial port that streams to the panel, so you can exercise the real
serial path: run `uv run --all-extras examples/fake_device.py` and pass the
port it prints to the dashboard with `--port`.
