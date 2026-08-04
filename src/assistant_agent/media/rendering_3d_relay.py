"""In-process relay for asynchronous 3D results to active media sockets."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


RelaySender = Callable[[dict[str, Any]], Awaitable[None]]
RelayFrameFactory = Callable[["Rendering3DRelayBinding"], dict[str, Any]]


@dataclass(frozen=True)
class Rendering3DRelayBinding:
    connection_id: str
    number: str
    sender: RelaySender


class Rendering3DRelayUnavailable(RuntimeError):
    """Raised when no active media connection owns a runtime session."""


class Rendering3DRelayRegistry:
    def __init__(self) -> None:
        self._bindings: dict[str, Rendering3DRelayBinding] = {}
        self._lock = asyncio.Lock()

    async def register(
        self,
        *,
        session_id: str,
        connection_id: str,
        number: str,
        sender: RelaySender,
    ) -> None:
        binding = Rendering3DRelayBinding(
            connection_id=connection_id,
            number=number,
            sender=sender,
        )
        async with self._lock:
            self._bindings[session_id] = binding

    async def unregister(self, *, session_id: str, connection_id: str) -> None:
        async with self._lock:
            binding = self._bindings.get(session_id)
            if binding is not None and binding.connection_id == connection_id:
                self._bindings.pop(session_id, None)

    async def send(
        self,
        session_id: str,
        frame_factory: RelayFrameFactory,
    ) -> Rendering3DRelayBinding:
        async with self._lock:
            binding = self._bindings.get(session_id)
        if binding is None:
            raise Rendering3DRelayUnavailable(session_id)
        await binding.sender(frame_factory(binding))
        return binding


_rendering_3d_relay_registry = Rendering3DRelayRegistry()


def get_rendering_3d_relay_registry() -> Rendering3DRelayRegistry:
    return _rendering_3d_relay_registry
