"""JSON WebSocket endpoint adapter for gateway frames."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Optional

from assistant_agent.gateway.protocol import Frame


class WsProtocolError(Exception):
    """Raised when a WebSocket message cannot be decoded as a frame."""


def dumps_frame(f: Frame) -> str:
    return json.dumps(f, ensure_ascii=False, separators=(",", ":"))


def loads_frame(s: str) -> Frame:
    try:
        obj = json.loads(s)
    except json.JSONDecodeError as exc:
        raise WsProtocolError(f"invalid json: {exc}") from exc
    if not isinstance(obj, dict):
        raise WsProtocolError("frame must be a JSON object")
    return obj  # type: ignore[return-value]


@dataclass
class WsEndpoint:
    """Present a text WebSocket as an Endpoint-like frame stream."""

    _ws: Any
    _send_lock: asyncio.Lock
    _pending: "asyncio.Queue[Optional[Frame]]"

    @classmethod
    def wrap(cls, ws: Any) -> "WsEndpoint":
        return cls(_ws=ws, _send_lock=asyncio.Lock(), _pending=asyncio.Queue())

    def _inject(self, f: Frame) -> None:
        self._pending.put_nowait(f)

    async def send(self, f: Frame) -> None:
        msg = dumps_frame(f)
        async with self._send_lock:
            await self._ws.send(msg)

    async def __aiter__(self) -> AsyncIterator[Frame]:
        while not self._pending.empty():
            item = self._pending.get_nowait()
            if item is not None:
                yield item
        async for msg in self._ws:
            if not isinstance(msg, str):
                raise WsProtocolError("only text frames are supported")
            yield loads_frame(msg)
