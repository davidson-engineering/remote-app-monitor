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
    """Subscribes to a ZeroMQ PUB socket and decodes each message.

    By default it connects to a publisher that binds ``endpoint`` and expects
    ``"<id> <value>"`` messages. Pass ``bind=True`` to bind instead (for
    several publishers connecting to one monitor).

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
        bind: bool = False,
    ) -> None:
        self.endpoint = endpoint
        self.decoder = decoder or KeyValueDecoder()
        self.bind = bind

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
        socket = zmq.Context.instance().socket(zmq.SUB)
        try:
            socket.setsockopt(zmq.LINGER, 0)
            socket.subscribe(b"")
            if self.bind:
                socket.bind(self.endpoint)
            else:
                socket.connect(self.endpoint)
            logger.info("Subscribed to %s", self.endpoint)
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
