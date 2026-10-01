"""Full-screen terminal display."""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import sys
import threading
from collections.abc import Callable
from typing import TextIO

from .elements import Element
from .formatting import Style, boxed, visible_len
from .monitor import Group, Monitor

Screen = Callable[[Monitor, int, int], str]
"""``screen(monitor, width, height)``: a whole frame, laid out by you."""

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

    By default every element is listed, ``width`` columns wide. For a layout
    of your own, the terminal's counterpart of a custom web page, pass
    ``screen``: a function of ``(monitor, width, height)`` that returns the
    whole frame, styled with :class:`Style` if you like, for a terminal of
    that size. Read values from the monitor's elements (``monitor["rate"]
    .text``). A screen is also redrawn at least once a second, so it can show
    time, e.g. how old values are (:meth:`Monitor.ages`).

    Uses the terminal's alternate screen, so the previous contents come back
    when it stops; the final frame is then printed so it stays in view.
    Anything else printed to the same terminal (e.g. logging to stdout) will be
    overdrawn; send logs to a file instead.
    """

    refresh = 1.0  # with a screen: seconds between redraws when nothing changes

    def __init__(
        self,
        *,
        width: int = 60,
        fps: float = 30,
        stream: TextIO | None = None,
        screen: Screen | None = None,
    ):
        if fps <= 0:
            raise ValueError("fps must be positive")
        self.width = width
        self.fps = fps
        self.stream = stream
        self.screen = screen
        self._writing = threading.Lock()  # one frame at a time, then the restore

    def render(self, monitor: Monitor) -> str:
        """The full frame as text (without terminal control codes)."""
        if self.screen is not None:
            size = shutil.get_terminal_size()
            frame = self.screen(monitor, size.columns, size.lines)
            return "\n".join(frame.split("\n")[: size.lines])  # never scroll
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
        # Writing blocks while the terminal isn't reading (Ctrl+S, a stalled
        # SSH link, a full pipe), so it happens on a thread: that holds up
        # this display, never the monitor's event loop and its other outputs.
        await asyncio.to_thread(self._write, out, ALT_SCREEN_ON + CURSOR_HIDE)
        try:
            version = -1
            while True:
                if self.screen is None:
                    version = await monitor.wait_for_change(version)
                else:
                    with contextlib.suppress(TimeoutError):
                        version = await asyncio.wait_for(
                            monitor.wait_for_change(version), self.refresh
                        )
                frame = CURSOR_HOME + self._frame(monitor)  # read on the loop
                await asyncio.to_thread(self._write, out, frame)
                await asyncio.sleep(1 / self.fps)
        finally:
            self._write(out, CURSOR_SHOW + ALT_SCREEN_OFF + self.render(monitor) + "\n")

    def _write(self, out: TextIO, text: str) -> None:
        with self._writing:
            out.write(text)
            out.flush()

    def _frame(self, monitor: Monitor) -> str:
        """The frame, with codes clearing what the last one left behind.

        A line that fills the width leaves the cursor on its last character,
        where clearing would erase it; those need no clearing anyway.
        """
        size = shutil.get_terminal_size()
        lines = self.render(monitor).split("\n")
        frame = "\n".join(
            line + CLEAR_LINE_END if visible_len(line) < size.columns else line
            for line in lines
        )
        if len(lines) < size.lines:  # clear any longer frame's leftovers below
            frame += "\n" + CLEAR_SCREEN_END
        return frame


def _render_element(element: Element, width: int) -> str:
    if element.border:
        return boxed(element.render(width - 4), width, element.label)
    if element.block:  # a table or log: label it with a heading line
        return f"{HEADING.apply(element.label)}\n{element.render(width)}"
    return element.render(width)
