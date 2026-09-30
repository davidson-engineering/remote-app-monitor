"""Simulated data showing every kind of element: ``sightglass --demo``."""

from __future__ import annotations

import math
import random
from typing import Any

from .elements import (
    IndicatorLamp,
    LogMonitor,
    MachineState,
    ProgressBar,
    RangeBar,
    Sparkline,
    Table,
    TextElement,
)
from .formatting import TextFormat
from .monitor import Monitor
from .sources.simulated import SimulatedSource

AXES = ["X", "Y", "Z"]


def build_monitor() -> Monitor:
    monitor = Monitor()
    monitor.add_group(
        "job",
        [
            ProgressBar("progress", label="Progress"),
            Sparkline("rate", label="Items/s", format=TextFormat(precision=1)),
            TextElement("status", label="Status"),
        ],
    )
    for axis in AXES:
        monitor.add_group(
            axis,
            [
                RangeBar(
                    "velocity", min_value=-10, max_value=10, units="mm/s", precision=1
                ),
                RangeBar("torque", min_value=-5, max_value=5, units="Nm", precision=1),
            ],
        )
    monitor.add(
        TextElement(
            "temperature",
            label="Temperature",
            units="°C",
            format=TextFormat(precision=1),
        ),
        IndicatorLamp("coolant", label="Coolant"),
        MachineState("machine", label="Machine", states=["enabled", "homed", "estop"]),
        Table(
            "error",
            label="Following error",
            rows=AXES,
            columns=["now", "max"],
            format=TextFormat(precision=3),
        ),
        LogMonitor("log", label="Log", lines=5, timestamp=True),
    )
    return monitor


class Simulation:
    """Plausible machine data as a function of time."""

    def __init__(self) -> None:
        self.worst = dict.fromkeys(AXES, 0.0)
        self.logged = -1

    def __call__(self, t: float) -> dict[str, Any]:
        progress = (t * 2) % 100
        sample: dict[str, Any] = {
            "job.progress": progress,
            "job.rate": 40 + 8 * math.sin(t / 3) + random.uniform(-2, 2),
            "job.status": "finishing" if progress > 90 else "machining",
            "temperature": 42 + 6 * math.sin(t / 20),
            "coolant": int(t) % 20 < 15,
            "machine": 0b011 if int(t) % 30 < 27 else 0b100,
        }
        for i, axis in enumerate(AXES):
            velocity = 8 * math.sin(0.7 * t + i)
            sample[f"{axis}.velocity"] = velocity
            sample[f"{axis}.torque"] = 0.4 * velocity + math.sin(5 * t + i)
            error = abs(math.sin(1.3 * t + i)) / 100
            self.worst[axis] = max(self.worst[axis], error)
            sample[f"error.{axis}.now"] = error
            sample[f"error.{axis}.max"] = self.worst[axis]
        if int(t // 5) != self.logged:
            self.logged = int(t // 5)
            sample["log"] = f"part {self.logged + 1} started"
        return sample


def source() -> SimulatedSource:
    return SimulatedSource(Simulation(), rate=20)
