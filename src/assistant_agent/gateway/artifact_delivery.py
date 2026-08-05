"""Gateway-owned delivery of neutral asynchronous artifact events."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ArtifactCompleted(BaseModel):
    """Entry-neutral notification that an asynchronous artifact is available."""

    model_config = ConfigDict(frozen=True)

    type: Literal["artifact.completed"] = "artifact.completed"
    artifact_id: str = Field(min_length=1)
    user_id: str | None = None
    session_id: str = Field(min_length=1)
    media_type: str = Field(min_length=1)
    uri: str | None = None
    inline_data: str | None = None


ArtifactSubscriber = Callable[[ArtifactCompleted], Awaitable[None]]


@dataclass(frozen=True)
class ArtifactDeliveryBinding:
    subscriber_id: str
    sender: ArtifactSubscriber


class GatewayArtifactDeliveryHub:
    """Route artifact events to the current subscriber for a session."""

    def __init__(self) -> None:
        self._bindings: dict[str, ArtifactDeliveryBinding] = {}
        self._lock = asyncio.Lock()

    async def register(
        self,
        *,
        session_id: str,
        subscriber_id: str,
        sender: ArtifactSubscriber,
    ) -> None:
        binding = ArtifactDeliveryBinding(
            subscriber_id=subscriber_id,
            sender=sender,
        )
        async with self._lock:
            self._bindings[session_id] = binding

    async def unregister(self, *, session_id: str, subscriber_id: str) -> None:
        async with self._lock:
            binding = self._bindings.get(session_id)
            if binding is not None and binding.subscriber_id == subscriber_id:
                self._bindings.pop(session_id, None)

    async def publish(self, event: ArtifactCompleted) -> bool:
        async with self._lock:
            binding = self._bindings.get(event.session_id)
        if binding is None:
            return False
        await binding.sender(event)
        return True


_artifact_delivery_hub = GatewayArtifactDeliveryHub()


def get_gateway_artifact_delivery_hub() -> GatewayArtifactDeliveryHub:
    return _artifact_delivery_hub
