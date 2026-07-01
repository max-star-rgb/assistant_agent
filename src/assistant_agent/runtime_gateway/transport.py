"""Transport-agnostic endpoint primitives for runtime gateway tests and embedding."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Optional

from assistant_agent.runtime_gateway.protocol import Frame


class Closed(Exception):
    """Raised when sending through a closed in-memory endpoint."""


@dataclass
class InMemoryDuplex:
    """A minimal bidirectional channel for Gateway<->Runtime frame streams."""

    _a_to_b: "asyncio.Queue[Optional[Frame]]"
    _b_to_a: "asyncio.Queue[Optional[Frame]]"

    @classmethod
    def create_pair(cls) -> tuple["Endpoint", "Endpoint"]:
        q1: "asyncio.Queue[Optional[Frame]]" = asyncio.Queue()
        q2: "asyncio.Queue[Optional[Frame]]" = asyncio.Queue()
        duplex = cls(_a_to_b=q1, _b_to_a=q2)
        return (Endpoint(duplex, side="a"), Endpoint(duplex, side="b"))


class Endpoint:
    def __init__(self, duplex: InMemoryDuplex, *, side: str) -> None:
        self._duplex = duplex
        self._side = side
        self._closed = False

    async def send(self, f: Frame) -> None:
        if self._closed:
            raise Closed()
        if self._side == "a":
            await self._duplex._a_to_b.put(f)
        else:
            await self._duplex._b_to_a.put(f)

    def _inject(self, f: Frame) -> None:
        """Inject a frame into this endpoint's read queue."""

        if self._side == "a":
            self._duplex._b_to_a.put_nowait(f)
        else:
            self._duplex._a_to_b.put_nowait(f)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._side == "a":
            await self._duplex._a_to_b.put(None)
        else:
            await self._duplex._b_to_a.put(None)

    async def __aiter__(self) -> AsyncIterator[Frame]:
        if self._side == "a":
            q = self._duplex._b_to_a
        else:
            q = self._duplex._a_to_b
        while True:
            item = await q.get()
            if item is None:
                break
            yield item
