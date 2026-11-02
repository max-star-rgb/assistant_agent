"""Connection-scoped delivery of neutral asynchronous media artifacts."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field


logger = logging.getLogger(__name__)


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
            logger.info(
                "media_artifact_no_subscriber chat=%s thread=%s",
                str(uuid5(NAMESPACE_URL, f"artifact:{event.artifact_id}"))[:12],
                str(uuid5(NAMESPACE_URL, event.session_id))[:12],
            )
            return False
        correlation = str(uuid5(NAMESPACE_URL, f"artifact:{event.artifact_id}"))[:12]
        logger.info(
            "media_artifact_dispatch chat=%s thread=%s connection=%s media_type=%s url_present=%s",
            correlation,
            str(uuid5(NAMESPACE_URL, event.session_id))[:12],
            str(uuid5(NAMESPACE_URL, binding.subscriber_id))[:12],
            event.media_type if event.media_type in {"mp4", "glb", "ply", "image"} else "unsupported",
            bool(event.uri),
        )
        await binding.sender(event)
        logger.info("media_artifact_sender_completed chat=%s", correlation)
        return True


_artifact_delivery_hub = MediaArtifactDeliveryHub()


def get_media_artifact_delivery_hub() -> MediaArtifactDeliveryHub:
    return _artifact_delivery_hub


__all__ = [
    "ArtifactCompleted",
    "MediaArtifactDeliveryHub",
    "get_media_artifact_delivery_hub",
]
