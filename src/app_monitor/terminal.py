"""Full-screen terminal display."""

from __future__ import annotations

import asyncio
import shutil
import sys
from typing import TextIO

from .elements import Element
from .formatting import Style, boxed
from .monitor import Group, Monitor

ALT_SCREEN_ON = "\x1b[?1049h"
ALT_SCREEN_OFF = "\x1b[?1049l"
CURSOR_HIDE = "\x1b[?25l"
CURSOR_SHOW = "\x1b[?25h"
CURSOR_HOME = "\x1b[H"
CLEAR_LINE_END = "\x1b[K"
CLEAR_SCREEN_END = "\x1b[J"

HEADING = Style(bold=True)


class TerminalDisplay:
    """Draws the monitor in the terminal, redrawing only when something changed
    and at most ``fps`` times per second.

    Uses the terminal's alternate screen, so the previous contents come back
    when it stops; the final frame is then printed so it stays in view.
    Anything else printed to the same terminal (e.g. logging to stdout) will be
    overdrawn; send logs to a file instead.
    """

    def __init__(
        self, *, width: int = 60, fps: float = 30, stream: TextIO | None = None
    ):
        if fps <= 0:
            raise ValueError("fps must be positive")
        self.width = width
        self.fps = fps
        self.stream = stream

    def render(self, monitor: Monitor) -> str:
        """The full frame as text (without terminal control codes)."""
        width = min(self.width, shutil.get_terminal_size().columns)
        blocks = []
        for item in monitor.layout:
            if isinstance(item, Group):
                inner = width - 4 if item.border else width
                body = "\n".join(_render_element(el, inner) for el in item.elements)
                blocks.append(boxed(body, width, item.name) if item.border else body)
            else:
                blocks.append(_render_element(item, width))
        return "\n".join(blocks)

    async def run(self, monitor: Monitor) -> None:
        out = self.stream or sys.stdout
        out.write(ALT_SCREEN_ON + CURSOR_HIDE)
        try:
            version = -1
            while True:
                version = await monitor.wait_for_change(version)
                frame = self.render(monitor).replace("\n", CLEAR_LINE_END + "\n")
                out.write(CURSOR_HOME + frame + CLEAR_LINE_END + CLEAR_SCREEN_END)
                out.flush()
                await asyncio.sleep(1 / self.fps)
        finally:
            out.write(CURSOR_SHOW + ALT_SCREEN_OFF + self.render(monitor) + "\n")
            out.flush()


def _render_element(element: Element, width: int) -> str:
    if element.border:
        return boxed(element.render(width - 4), width, element.label)
    if element.block:  # a table or log: label it with a heading line
        return f"{HEADING.apply(element.label)}\n{element.render(width)}"
    return element.render(width)
