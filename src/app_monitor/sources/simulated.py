"""A source that generates values, for demos and testing without hardware."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

from ..monitor import Update


class SimulatedSource:
    """Calls ``generate(seconds_elapsed)`` ``rate`` times per second and feeds
    the returned mapping into the monitor."""

    def __init__(self, generate: Callable[[float], Update], rate: float = 20) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.generate = generate
        self.rate = rate

    async def updates(self) -> AsyncIterator[list[Update]]:
        loop = asyncio.get_running_loop()
        start = loop.time()
        while True:
            yield [self.generate(loop.time() - start)]
            await asyncio.sleep(1 / self.rate)

    def __repr__(self) -> str:
        return f"SimulatedSource(rate={self.rate})"
