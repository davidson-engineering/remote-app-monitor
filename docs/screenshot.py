"""Take the README's screenshots of the launch demo, in a browser and a terminal.

    uv run --all-extras python docs/screenshot.py

Runs `sightglass --demo launch --terminal` from the start of its countdown in
a 120 x 36 pseudo-terminal, as someone trying it would (on port 8080, as the
README shows). 52 seconds after liftoff (about 80 s from now), just past
maximum aerodynamic pressure, it photographs the page the demo serves and
draws what it printed with xterm.js, a real terminal emulator (fetched from
jsDelivr). Needs Chromium once: uv run playwright install chromium. macOS
and Linux (it uses a pty).
"""

import fcntl
import json
import os
import pty
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

DOCS = Path(__file__).parent
MOMENT = 30 + 52  # seconds from starting: the countdown, then 52 s of flight
COLUMNS, LINES = 120, 36

TERMINAL = """<!doctype html>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/css/xterm.css">
<script src="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/lib/xterm.js"></script>
<script src="https://cdn.jsdelivr.net/npm/@xterm/addon-webgl@0.18.0/lib/addon-webgl.js"></script>
<style>
  body { margin: 0 }
  #t { display: inline-block; padding: 28px; background: #0a0c0f }
</style>
<div id="t"></div>
<script>
  const term = new Terminal({
    cols: COLUMNS, rows: LINES, fontSize: 28, lineHeight: 1,
    fontFamily: "Menlo, 'DejaVu Sans Mono', monospace",
    theme: { background: "#0a0c0f", foreground: "#d0d0d0" },
  });
  term.open(document.getElementById("t"));
  term.loadAddon(new WebglAddon.WebglAddon());  // exact block and line glyphs
  term.write(FRAME, () => { window.drawn = true; });
</script>
"""


def main() -> None:
    controller, terminal = pty.openpty()
    size = struct.pack("HHHH", LINES, COLUMNS, 0, 0)
    fcntl.ioctl(terminal, termios.TIOCSWINSZ, size)
    env = {k: v for k, v in os.environ.items() if k not in ("COLUMNS", "LINES")}
    demo = subprocess.Popen(
        [sys.executable, "-m", "sightglass", "--demo", "launch", "--terminal"],
        stdin=terminal,
        stdout=terminal,
        stderr=terminal,
        cwd=DOCS,  # where its sightglass.log goes (removed afterwards)
        env={**env, "TERM": "xterm-256color", "PYTHONUNBUFFERED": "1"},
        start_new_session=True,
    )
    os.close(terminal)
    started = time.monotonic()
    printed = bytearray()

    def read() -> None:  # everything the demo prints to its terminal
        while True:
            try:
                chunk = os.read(controller, 65536)
            except OSError:  # it closed the terminal
                return
            if not chunk:
                return
            printed.extend(chunk)

    threading.Thread(target=read, daemon=True).start()
    try:
        while b"Dashboard: " not in printed:
            time.sleep(0.1)
        url = printed.split(b"Dashboard: ")[1].split()[0].decode()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(
                viewport={"width": 1440, "height": 900}, device_scale_factor=2
            )
            page.goto(url)
            time.sleep(MOMENT - (time.monotonic() - started))
            page.screenshot(path=DOCS / "launch-control.png")
            # The latest whole frame: each one starts by moving the cursor home.
            frame = printed.decode("utf-8", "replace").split("\x1b[H")[-2]

            view = browser.new_page(viewport={"width": 2400, "height": 1600})
            html = TERMINAL.replace("COLUMNS", str(COLUMNS)).replace(
                "LINES", str(LINES)
            )
            view.set_content(html.replace("FRAME", json.dumps(frame)))
            view.wait_for_function("window.drawn === true")
            view.locator("#t").screenshot(path=DOCS / "launch-terminal.png")
            browser.close()
    finally:
        os.killpg(demo.pid, signal.SIGINT)  # Ctrl+C
        demo.wait(10)
        os.close(controller)
        (DOCS / "sightglass.log").unlink(missing_ok=True)
    print("Saved docs/launch-control.png and docs/launch-terminal.png")


if __name__ == "__main__":
    main()
