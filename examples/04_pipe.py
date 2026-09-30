# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Pipe a program into a dashboard: no dashboard code in the program at all.

    uv run https://raw.githubusercontent.com/davidson-engineering/sightglass/main/examples/04_pipe.py | uvx --from "sightglass[web] @ git+https://github.com/davidson-engineering/sightglass" sightglass --open

(or, with the package installed:  python examples/04_pipe.py | sightglass --open)

This pretend data pipeline only prints. Lines made of key=value pairs become
live values on the dashboard; every other line still shows in the terminal.
"""

import random
import time

STAGES = ["download", "parse", "validate", "load"]

try:
    for run in range(1, 1000):
        print(f"Run {run}: starting")  # ordinary output, passed through
        for stage in STAGES:
            for step in range(1, 21):
                time.sleep(0.05)
                rows = random.randint(900, 1100)
                print(
                    f"run={run} stage={stage} step={step}/20 rows_per_s={rows}",
                    flush=True,
                )
            print(f"Run {run}: {stage} finished")
        errors = random.choice([0, 0, 0, 1])
        print(f'errors={errors} last_run="{time.strftime("%H:%M:%S")}"', flush=True)
except KeyboardInterrupt:
    pass
