"""Connection-scoped delivery of neutral asynchronous media artifacts."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ArtifactCompleted(BaseModel):
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


class MediaArtifactDeliveryHub:
    """Route an artifact to the current online connection for a native thread."""

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
        async with self._lock:
            self._bindings[session_id] = ArtifactDeliveryBinding(
                subscriber_id=subscriber_id,
                sender=sender,
            )

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


_artifact_delivery_hub = MediaArtifactDeliveryHub()


def get_media_artifact_delivery_hub() -> MediaArtifactDeliveryHub:
    return _artifact_delivery_hub


__all__ = [
    "ArtifactCompleted",
    "MediaArtifactDeliveryHub",
    "get_media_artifact_delivery_hub",
]
