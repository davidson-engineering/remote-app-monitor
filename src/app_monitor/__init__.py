"""Live web and terminal dashboards for values streamed from devices.

A :class:`Monitor` holds elements (text, bars, lamps, tables, ...). Sources
(serial, ZeroMQ, simulated) feed it updates keyed by element id, and outputs
(web dashboard, terminal) display it::

    monitor = Monitor()
    monitor.add(TextElement("temperature", units="°C"))
    await monitor.run(
        sources=[SerialSource("/dev/ttyUSB0", decoder=CsvDecoder(["temperature"]))],
        outputs=[WebDashboard()],
    )
"""

import importlib
import logging
from typing import TYPE_CHECKING, Any

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
    Table,
    TextElement,
)
from .formatting import Style, TextFormat
from .monitor import Group, Monitor, Output, Source, Update
from .sources.simulated import SimulatedSource
from .terminal import TerminalDisplay

if TYPE_CHECKING:
    from .sources.serialport import SerialSource, find_serial_port
    from .sources.zeromq import ZmqSource
    from .web import WebDashboard

__version__ = "0.2.0"

logging.getLogger(__name__).addHandler(logging.NullHandler())

# Classes that need an optional dependency are imported on first use.
_OPTIONAL = {
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
    except ImportError as error:
        raise ImportError(
            f"{name} needs an optional dependency: "
            f"pip install 'remote-app-monitor[{extra}]' ({error})"
        ) from error
    globals()[name] = value
    return value


__all__ = [
    "BinaryFrameDecoder",
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
    "Style",
    "Table",
    "TerminalDisplay",
    "TextElement",
    "TextFormat",
    "Update",
    "WebDashboard",
    "ZmqSource",
    "find_serial_port",
]
