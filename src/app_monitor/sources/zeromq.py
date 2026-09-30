"""ZeroMQ subscriber source (requires ``pyzmq``)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import zmq
import zmq.asyncio

from ..decoders import Decoder, KeyValueDecoder
from ..monitor import Update

logger = logging.getLogger(__name__)


class ZmqSource:
    """Subscribes to a ZeroMQ PUB socket and decodes each message.

    By default it connects to a publisher that binds ``endpoint`` and expects
    ``"<id> <value>"`` messages. Pass ``bind=True`` to bind instead (for
    several publishers connecting to one monitor).
    """

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
        socket = zmq.asyncio.Context.instance().socket(zmq.SUB)
        socket.setsockopt(zmq.LINGER, 0)
        socket.subscribe(b"")
        try:
            if self.bind:
                socket.bind(self.endpoint)
            else:
                socket.connect(self.endpoint)
            logger.info("Subscribed to %s", self.endpoint)
            while True:
                if batch := self.decoder.decode(await socket.recv()):
                    yield batch
        finally:
            socket.close()

    def __repr__(self) -> str:
        return f"ZmqSource({self.endpoint!r})"
