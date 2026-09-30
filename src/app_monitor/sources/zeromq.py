"""ZeroMQ subscriber source (requires ``pyzmq``)."""

from __future__ import annotations

import logging
import threading
from collections.abc import AsyncIterator
from contextlib import aclosing

import zmq

from ..decoders import Decoder, KeyValueDecoder
from ..monitor import Update
from ._thread import Emit, thread_items

logger = logging.getLogger(__name__)


class ZmqSource:
    """Receives ZeroMQ messages and decodes each one.

    ``pattern="sub"`` (default) subscribes to a PUB socket, connecting to a
    publisher that binds ``endpoint``. PUB/SUB drops messages sent before the
    subscriber has joined, so a short-lived program can lose everything.

    ``pattern="pull"`` binds ``endpoint`` and receives from PUSH sockets that
    connect to it. PUSH queues messages until they are delivered, so nothing
    is lost, and any number of programs can push to one monitor.

    ``bind`` overrides who binds. Messages are ``"<id> <value>"`` (or
    ``key=value`` pairs) unless another ``decoder`` is given.

    Messages are received on a background thread, all waiting messages at
    once, so bursts are applied together and any event loop works (including
    Windows' default one, which zmq.asyncio does not support).
    """

    poll_interval = 0.1
    max_batch = 1000  # hand over at least this often during a flood

    def __init__(
        self,
        endpoint: str = "tcp://localhost:5556",
        *,
        decoder: Decoder | None = None,
        pattern: str = "sub",
        bind: bool | None = None,
    ) -> None:
        if pattern not in ("sub", "pull"):
            raise ValueError(f"pattern must be 'sub' or 'pull', not {pattern!r}")
        self.endpoint = endpoint
        self.decoder = decoder or KeyValueDecoder()
        self.pattern = pattern
        self.bind = pattern == "pull" if bind is None else bind

    async def updates(self) -> AsyncIterator[list[Update]]:
        async with aclosing(thread_items(self._receive_forever, repr(self))) as items:
            async for messages in items:
                batch = [
                    update
                    for message in messages
                    for update in self.decoder.decode(message)
                ]
                if batch:
                    yield batch

    def _receive_forever(self, emit: Emit, stop: threading.Event) -> None:
        """Background thread: receive until ``stop`` is set."""
        kind = zmq.SUB if self.pattern == "sub" else zmq.PULL
        socket = zmq.Context.instance().socket(kind)
        try:
            socket.setsockopt(zmq.LINGER, 0)
            if kind == zmq.SUB:
                socket.subscribe(b"")
            if self.bind:
                socket.bind(self.endpoint)
            else:
                socket.connect(self.endpoint)
            logger.info("Receiving from %s (%s)", self.endpoint, self.pattern)
            timeout_ms = int(self.poll_interval * 1000)
            while not stop.is_set():
                if not socket.poll(timeout_ms):
                    continue
                messages = []
                while len(messages) < self.max_batch:
                    try:
                        messages.append(socket.recv(zmq.NOBLOCK))
                    except zmq.Again:
                        break
                emit(messages)
        finally:
            socket.close()

    def __repr__(self) -> str:
        return f"ZmqSource({self.endpoint!r})"
