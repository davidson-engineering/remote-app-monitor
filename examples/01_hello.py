# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "remote-app-monitor[web] @ git+https://github.com/davidson-engineering/remote-app-monitor",
# ]
# ///
"""Hello, dashboard: live values from a Python program, with nothing to set up.

    uv run https://raw.githubusercontent.com/davidson-engineering/remote-app-monitor/main/examples/01_hello.py

A pretend batch job reports what it's doing. Nothing is declared: each value
gets a display the first time it is set. Stop with Ctrl+C.
"""

import itertools
import random
import time

from app_monitor import WebDashboard, start

monitor = start(outputs=[WebDashboard(title="Hello, dashboard", open_browser=True)])

try:
    for batch in itertools.count(1):
        monitor.set("batch", batch)
        for item in range(1, 101):
            time.sleep(0.05)  # the program's real work goes here
            monitor.set("progress", f"{item}%")
            monitor.set("items per second", random.uniform(18, 22))
            monitor.set("status", "finishing" if item > 90 else "working")
            monitor.set("healthy", random.random() > 0.02)  # True/False: a lamp
except KeyboardInterrupt:
    pass
