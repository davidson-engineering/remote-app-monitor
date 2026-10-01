"""Live dashboards for values from your program, a device, or any process.

Quickest start, from inside a program::

    from sightglass import start

    monitor = start()                  # prints the dashboard's address
    monitor.set("progress", 0.5)       # from any thread; elements appear as used

A :class:`Monitor` holds elements (text, bars, lamps, charts, tables, ...).
Sources (serial, ZeroMQ, PLCs over ADS, stdin, simulated) and HTTP posts feed it updates
keyed by element id, and outputs (web dashboard, terminal) display it. Other
programs can send values with :class:`~sightglass.client.Client`, or with
``curl -d 'progress=5' http://127.0.0.1:8080/update``.
"""

import importlib
from typing import TYPE_CHECKING, Any

from .client import Client
from .decoders import (
    BinaryFrameDecoder,
    CsvDecoder,
    DecodeError,
    Decoder,
    JsonDecoder,
    KeyValueDecoder,
    LineDecoder,
)
from .elements import (
    Coordinate,
    Element,
    IndicatorLamp,
    LogMonitor,
    MachineState,
    ProgressBar,
    RangeBar,
    Sparkline,
    Table,
    TextElement,
)
from .formatting import Style, TextFormat
from .monitor import Group, Monitor, Output, Source, Update, WriteError, start
from .sources.simulated import SimulatedSource
from .sources.stdin import StdinSource
from .terminal import TerminalDisplay

if TYPE_CHECKING:
    from .sources.ads import AdsSource
    from .sources.serialport import SerialSource, find_serial_port
    from .sources.zeromq import ZmqSource
    from .web import WebDashboard

__version__ = "0.2.0"

# Classes that need an optional dependency are imported on first use.
_OPTIONAL = {
    "AdsSource": (".sources.ads", "ads"),
    "SerialSource": (".sources.serialport", "serial"),
    "find_serial_port": (".sources.serialport", "serial"),
    "ZmqSource": (".sources.zeromq", "zmq"),
    "WebDashboard": (".web", "web"),
}


def __getattr__(name: str) -> Any:
    if name not in _OPTIONAL:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module, extra = _OPTIONAL[name]
    try:
        value = getattr(importlib.import_module(module, __name__), name)
    except ModuleNotFoundError as error:
        raise ImportError(
            f"{name} needs an optional dependency: "
            f"pip install 'sightglass[{extra}]' ({error})"
        ) from error
    globals()[name] = value
    return value


__all__ = [
    "AdsSource",
    "BinaryFrameDecoder",
    "Client",
    "Coordinate",
    "CsvDecoder",
    "DecodeError",
    "Decoder",
    "Element",
    "Group",
    "IndicatorLamp",
    "JsonDecoder",
    "KeyValueDecoder",
    "LineDecoder",
    "LogMonitor",
    "MachineState",
    "Monitor",
    "Output",
    "ProgressBar",
    "RangeBar",
    "SerialSource",
    "SimulatedSource",
    "Source",
    "Sparkline",
    "StdinSource",
    "Style",
    "Table",
    "TerminalDisplay",
    "TextElement",
    "TextFormat",
    "Update",
    "WebDashboard",
    "WriteError",
    "ZmqSource",
    "find_serial_port",
    "start",
]
