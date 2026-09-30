"""``sightglass``: show live values without writing any code.

my_program | sightglass                     # key=value or JSON lines on stdout
sightglass                                  # then: curl -d 'temp=21.5' .../update
sightglass --serial auto --csv temp,humidity
sightglass --zmq tcp://localhost:5556
sightglass --demo
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from importlib import resources

from . import __version__
from .decoders import (
    BinaryFrameDecoder,
    CsvDecoder,
    Decoder,
    JsonDecoder,
    KeyValueDecoder,
)
from .monitor import Monitor, Output, Source
from .sources.stdin import StdinSource

EXAMPLES = """\
examples:
  my_program | sightglass               pipe a program's output: lines like
                                         "progress=5 status=running" or JSON
                                         objects become values; other lines are
                                         printed as usual
  sightglass                            then POST values from anything:
                                         curl -d temp=21.5 localhost:8080/update
  sightglass --serial auto --csv temperature,humidity
  sightglass --serial /dev/ttyUSB0 --binary 1=X,2=Y,10=velocity
  sightglass --zmq tcp://localhost:5556
  sightglass --zmq tcp://*:5557 --pull  receive from PUSH sockets (none are lost)
  sightglass --demo                     simulated data, every kind of element
  sightglass --demo launch --open       the showcase: a rocket launch, live
"""


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sightglass",
        description="Live dashboard for values from your program, a device, or "
        "anything that can make an HTTP request.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument(
        "--guide",
        action="store_true",
        help="print a concise guide to building monitors (written for AI coding "
        "agents) and exit",
    )

    inputs = p.add_argument_group(
        "inputs (stdin is read when it's a pipe; the dashboard also accepts "
        "POST /update)"
    )
    inputs.add_argument("--serial", metavar="PORT", help='serial device, or "auto"')
    inputs.add_argument(
        "--baudrate", type=int, default=115200, help="(default: 115200)"
    )
    serial_format = inputs.add_mutually_exclusive_group()
    serial_format.add_argument(
        "--csv",
        metavar="NAMES",
        help="device sends comma-separated values, in this order",
    )
    serial_format.add_argument(
        "--json", action="store_true", help="device sends JSON objects, one per line"
    )
    serial_format.add_argument(
        "--binary",
        metavar="MAP",
        help="device sends binary frames; MAP names the id bytes, e.g. 1=X,10=velocity",
    )
    inputs.add_argument("--zmq", metavar="ENDPOINT", help="receive ZeroMQ messages")
    inputs.add_argument(
        "--pull",
        action="store_true",
        help="with --zmq: bind a PULL socket instead of SUB",
    )
    inputs.add_argument(
        "--demo",
        nargs="?",
        const="elements",
        choices=["elements", "launch"],
        metavar="launch",
        help="show simulated data: every kind of element on the generic page, or "
        "with 'launch', a rocket launch on a hand-built page, fed by three programs",
    )

    display = p.add_argument_group("display")
    display.add_argument("--port", type=int, default=8080, help="(default: 8080)")
    display.add_argument(
        "--host",
        default="127.0.0.1",
        help="interface to listen on; 0.0.0.0 allows other machines",
    )
    display.add_argument("--title", default="Monitor", help="page heading")
    display.add_argument(
        "--open", action="store_true", help="open the dashboard in a browser"
    )
    display.add_argument("--token", help="require this token to POST /update")
    display.add_argument(
        "--stale-after",
        type=float,
        metavar="SECONDS",
        help="dim values when no data has arrived for this long",
    )
    display.add_argument(
        "--terminal",
        action="store_true",
        help="draw in this terminal instead of serving a web page "
        "(log messages go to sightglass.log)",
    )
    display.add_argument("-v", "--verbose", action="store_true", help="log more detail")
    return p


def build(args: argparse.Namespace) -> tuple[Monitor, list[Source], list[Output]]:
    """The monitor, sources and outputs the arguments describe."""
    sources: list[Source] = []
    if args.demo:
        from . import demo

        monitor = demo.build_monitor()
        sources.append(demo.source())
    else:
        monitor = Monitor()

    if args.serial:
        from . import SerialSource

        sources.append(
            SerialSource(args.serial, args.baudrate, decoder=serial_decoder(args))
        )
    elif args.csv or args.json or args.binary:
        raise ValueError("--csv, --json and --binary describe a --serial device")

    if args.zmq:
        from . import ZmqSource

        sources.append(ZmqSource(args.zmq, pattern="pull" if args.pull else "sub"))
    elif args.pull:
        raise ValueError("--pull goes with --zmq")

    if sys.stdin is not None and not sys.stdin.isatty() and not args.demo:
        stdin = StdinSource(echo=not args.terminal)
        stdin.on_end = lambda: _input_ended(stdin)
        sources.append(stdin)

    outputs: list[Output] = []
    if args.terminal:
        from .terminal import TerminalDisplay

        outputs.append(TerminalDisplay())
    else:
        from . import WebDashboard

        outputs.append(
            WebDashboard(
                host=args.host,
                port=args.port,
                title=args.title,
                token=args.token,
                stale_after=args.stale_after,
                open_browser=args.open,
            )
        )
    return monitor, sources, outputs


def launch(args: argparse.Namespace) -> None:
    """The launch demo: its own page and data, until Ctrl+C."""
    ignored = {
        "--serial": args.serial,
        "--csv": args.csv,
        "--json": args.json,
        "--binary": args.binary,
        "--zmq": args.zmq,
        "--pull": args.pull,
        "--title": args.title != "Monitor",
        "--token": args.token,
        "--stale-after": args.stale_after,
        "--terminal": args.terminal,
    }
    if given := [flag for flag, value in ignored.items() if value]:
        raise ValueError(
            f"--demo launch has its own page and data; drop {', '.join(given)}"
        )
    from .launch import run

    run(host=args.host, port=args.port, open_browser=args.open)


def guide() -> str:
    """The agent guide shipped with the package (GUIDE.md)."""
    return resources.files("sightglass").joinpath("GUIDE.md").read_text("utf-8")


def _input_ended(stdin: StdinSource) -> None:
    if not stdin.lines_read:  # e.g. started with </dev/null: nothing to report
        return
    print(
        "sightglass: input ended; still showing the last values (Ctrl+C to quit)",
        file=sys.stderr,
        flush=True,
    )


def serial_decoder(args: argparse.Namespace) -> Decoder:
    if args.csv:
        return CsvDecoder([name.strip() for name in args.csv.split(",")])
    if args.json:
        return JsonDecoder()
    if args.binary:
        names = {}
        for pair in args.binary.split(","):
            byte, sep, name = pair.partition("=")
            if not sep or not byte.strip().isdigit():
                raise ValueError(f"--binary expects BYTE=NAME pairs, got {pair!r}")
            names[int(byte)] = name.strip()
        return BinaryFrameDecoder(names)
    return KeyValueDecoder()


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.guide:
        print(guide(), end="")
        return 0
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
        # The terminal display would be drawn over any log output.
        filename="sightglass.log" if args.terminal else None,
    )
    try:
        if args.demo == "launch":
            launch(args)
        else:
            monitor, sources, outputs = build(args)
            monitor.serve(sources=sources, outputs=outputs)
    except (ImportError, OSError, ValueError) as error:
        message = error.strerror if isinstance(error, OSError) else None
        message = message or str(error)
        if "already in use" in message:
            message += f", e.g. --port {args.port + 1}"
        print(f"sightglass: error: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
