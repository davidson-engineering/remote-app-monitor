# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "remote-app-monitor[web] @ git+https://github.com/davidson-engineering/remote-app-monitor",
# ]
# ///
"""Many processes, one dashboard: workers report with the HTTP client.

    uv run https://raw.githubusercontent.com/davidson-engineering/remote-app-monitor/main/examples/05_many_processes.py

The main process serves the dashboard; four worker processes send their
progress with `Client`, which needs nothing but the standard library (in a
real system the workers could be other programs, on other machines).
Stop with Ctrl+C.
"""

import multiprocessing
import random
import time

from app_monitor import (
    Client,
    LogMonitor,
    Monitor,
    ProgressBar,
    TextElement,
    WebDashboard,
)

WORKERS = 4


def work(number: int, url: str) -> None:
    """One worker: a different program, as far as the dashboard is concerned."""
    dashboard = Client(url)
    name = f"worker {number}"
    parent = multiprocessing.parent_process()
    try:
        for job in range(1, 1000):
            dashboard.set(f"{name}.job", job)
            dashboard.set("log", f"{name} started job {job}")
            for percent in range(0, 101, 5):
                time.sleep(random.uniform(0.02, 0.2) * number)
                if not parent.is_alive():  # the dashboard process was stopped
                    return
                dashboard.set(f"{name}.progress", percent)
            dashboard.set("log", f"{name} finished job {job}")
    except KeyboardInterrupt:  # Ctrl+C reaches every process
        pass
    finally:
        dashboard.close(timeout=0)


if __name__ == "__main__":
    monitor = Monitor()
    for number in range(1, WORKERS + 1):
        monitor.add_group(
            f"worker {number}",
            [
                ProgressBar("progress", label="Progress"),
                TextElement("job", label="Current job"),
            ],
        )
    monitor.add(LogMonitor("log", label="Events", lines=8, timestamp=True))
    web = WebDashboard(title="Workers", open_browser=True)
    monitor.start(outputs=[web])

    workers = [
        multiprocessing.Process(target=work, args=(number, web.url), daemon=True)
        for number in range(1, WORKERS + 1)
    ]
    for worker in workers:
        worker.start()
    try:
        for worker in workers:
            worker.join()
    except KeyboardInterrupt:
        pass
