"""Run blocking I/O on a background thread and hand its results to asyncio.

Sources use this rather than asyncio-native I/O so they behave the same on
every event loop (Windows' default loop can't watch serial ports or ZeroMQ
sockets) and a stalled device can never block the loop.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Callable

Emit = Callable[[object], None]

_FINISHED = object()


async def thread_items(
    work: Callable[[Emit, threading.Event], None], name: str, *, join: bool = True
) -> AsyncIterator[object]:
    """Run ``work(emit, stop)`` on a daemon thread and yield what it emits.

    ``work`` should return soon after ``stop`` is set. If it raises, the
    exception is raised here. Closing the iterator sets ``stop`` and, if
    ``join``, waits for the thread to finish. Pass ``join=False`` for work that
    can block indefinitely (like reading stdin); its thread ends with the
    process.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[object] = asyncio.Queue()
    stop = threading.Event()

    def emit(item: object) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, item)
        except RuntimeError:  # the event loop has closed
            stop.set()

    def run() -> None:
        try:
            work(emit, stop)
        except Exception as error:
            emit(error)
        else:
            emit(_FINISHED)

    thread = threading.Thread(target=run, name=name, daemon=True)
    thread.start()
    try:
        while True:
            item = await queue.get()
            if item is _FINISHED:
                return
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        stop.set()
        if join:
            await asyncio.to_thread(thread.join)
