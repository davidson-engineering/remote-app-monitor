"""Standard input source, for piping a program's output into a monitor:

my_program | app-monitor
"""

from __future__ import annotations

import sys
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from typing import Any, TextIO

from ..decoders import DecodeError, JsonDecoder, LineDecoder, parse_pairs
from ..monitor import Update
from ._thread import Emit, thread_items


class StdinSource:
    """Reads lines from a stream (standard input by default).

    By default a line is an update if it is a JSON object
    (``{"progress": 5}``) or made up entirely of ``key=value`` pairs
    (``progress=5 status="loading data"``). Other lines are ordinary program
    output: with ``echo`` they are printed unchanged, so piping a program
    through the monitor doesn't hide its output.

    Pass a ``decoder`` (e.g. ``CsvDecoder``) to read a different format; lines
    it rejects are echoed the same way. The source ends at end of input
    (calling ``on_end``); the outputs keep showing the last values.
    """

    def __init__(
        self,
        *,
        decoder: LineDecoder | None = None,
        echo: bool = True,
        stream: TextIO | None = None,
        output: TextIO | None = None,
        on_end: Callable[[], None] | None = None,
    ) -> None:
        self.decoder = decoder
        self.echo = echo
        self.stream = stream
        self.output = output
        self.on_end = on_end
        self.lines_read = 0

    async def updates(self) -> AsyncIterator[list[Update]]:
        # Reading a pipe can block indefinitely, so the thread isn't joined.
        reader = thread_items(self._read, "app_monitor stdin", join=False)
        async with aclosing(reader) as updates:
            async for update in updates:
                yield [update]

    def parse(self, line: str) -> dict[str, Any] | None:
        """The update in ``line``, or None if it is ordinary output."""
        text = line.strip()
        if not text:
            return None
        if self.decoder is not None:
            try:
                return self.decoder.decode_line(text) or None
            except DecodeError:
                return None
        if text.startswith("{"):
            try:
                return JsonDecoder().decode_line(text)
            except DecodeError:
                return None
        return parse_pairs(text)

    def _read(self, emit: Emit, stop: threading.Event) -> None:
        stream = self.stream or sys.stdin
        output = self.output or sys.stdout
        for line in stream:
            if stop.is_set():
                return
            self.lines_read += 1
            if (update := self.parse(line)) is not None:
                emit(update)
            elif self.echo:
                output.write(line if line.endswith("\n") else line + "\n")
                output.flush()
        if self.on_end is not None:
            self.on_end()

    def __repr__(self) -> str:
        return "StdinSource()"
