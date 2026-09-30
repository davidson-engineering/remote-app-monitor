"""Take the README's screenshot of the launch control example.

    uv run --all-extras python docs/screenshot.py

Runs the example from the start of its countdown, as someone trying it would,
and photographs the page 52 seconds after liftoff (about 80 s from now), just
past maximum aerodynamic pressure. Needs Chromium once:
uv run playwright install chromium.
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).parent.parent
EXAMPLE = ROOT / "examples" / "launch_control" / "launch_control.py"
OUTPUT = ROOT / "docs" / "launch-control.png"
MOMENT = 30 + 52  # seconds from starting: the countdown, then 52 s of flight


def main() -> None:
    example = subprocess.Popen(
        [sys.executable, str(EXAMPLE)],
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "BROWSER": "true"},  # we have our own browser
        start_new_session=True,
    )
    started = time.monotonic()
    try:
        url = example.stderr.readline().split()[1]  # "Dashboard: http://..."
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(
                viewport={"width": 1440, "height": 900}, device_scale_factor=2
            )
            page.goto(url)
            page.wait_for_timeout((MOMENT - (time.monotonic() - started)) * 1000)
            page.screenshot(path=OUTPUT)
            browser.close()
    finally:
        os.killpg(example.pid, signal.SIGINT)  # Ctrl+C
        example.wait(10)
    print(f"Saved {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
