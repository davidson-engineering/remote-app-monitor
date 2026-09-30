"""``python -m sightglass.launch``: the launch demo, with a start time.

``sightglass --demo launch`` is the usual way to run it; this adds ``--at``,
e.g. ``--at 60`` to start a minute after liftoff.
"""

import argparse
import sys

from . import run
from .mission import COUNTDOWN

parser = argparse.ArgumentParser(
    prog="python -m sightglass.launch", description=__doc__.split("\n")[0]
)
parser.add_argument("--host", default="127.0.0.1", help="0.0.0.0 allows other machines")
parser.add_argument("--port", type=int, default=8080, help="(default: 8080; 0: any)")
parser.add_argument(
    "--at",
    type=float,
    default=-COUNTDOWN,
    metavar="SECONDS",
    help="start this long after liftoff (default: at the start of the countdown)",
)
parser.add_argument("--open", action="store_true", help="open it in a browser")
args = parser.parse_args()
try:
    run(host=args.host, port=args.port, at=args.at, open_browser=args.open)
except OSError as error:  # e.g. the port is taken: one line, not a traceback
    sys.exit(f"python -m sightglass.launch: error: {error.strerror or error}")
