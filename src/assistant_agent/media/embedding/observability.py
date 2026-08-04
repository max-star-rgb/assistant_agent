"""Prompt-safe, content-free observability for multimodal embedding lifecycle."""

from __future__ import annotations

import hashlib
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from assistant_agent.media.embedding.models import (
    EmbeddingEvent,
    EmbeddingFailureEvent,
    ImageObservation,
    TextObservation,
)


EMBEDDING_EVENT_NAMES = (
    "embedding.requested",
    "embedding.deduplicated",
    "embedding.started",
    "embedding.finished",
    "embedding.failed",
    "embedding.dispatched",
    "embedding.consumer_dropped",
    "embedding.session_cleanup",
)


class EmbeddingTraceEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_name: str = Field(pattern=r"^embedding\.")
    payload: dict[str, Any] = Field(default_factory=dict)


class EmbeddingObserver(Protocol):
    def record(self, event: EmbeddingTraceEvent) -> None: ...


class InMemoryEmbeddingObserver:
    def __init__(self) -> None:
        self.events: list[EmbeddingTraceEvent] = []

    def record(self, event: EmbeddingTraceEvent) -> None:
        self.events.append(event.model_copy(deep=True))


def embedding_trace_payload(
    *,
    outcome: EmbeddingEvent | EmbeddingFailureEvent | None = None,
    observation: ImageObservation | TextObservation | None = None,
    priority: str | None = None,
    consumer_id: str | None = None,
    consumer_count: int | None = None,
    cache_hit: bool | None = None,
) -> dict[str, Any]:
    """Project lifecycle facts without vectors, text, paths, or raw identities."""

    payload: dict[str, Any] = {}
    if observation is not None:
        payload.update(
            {
                "modality": "image" if isinstance(observation, ImageObservation) else "text",
                "session_id_digest": _digest(observation.session_id),
                "observation_id_digest": _digest(observation.observation_id),
            }
        )
        if isinstance(observation, ImageObservation):
            payload.update(
                {
                    "frame_sequence": observation.frame_sequence,
                    "captured_at_ms": observation.captured_at_ms,
                }
            )
        else:
            payload.update(
                {
                    "text_source": observation.source,
                    "occurred_at_ms": observation.occurred_at_ms,
                    "final": observation.final,
                }
            )
    if outcome is not None:
        payload.update(
            {
                "modality": outcome.modality,
                "session_id_digest": _digest(outcome.session_id),
                "observation_id_digest": _digest(outcome.source_observation_id),
                "latency_ms": outcome.latency_ms,
            }
        )
        if isinstance(outcome, EmbeddingEvent):
            payload.update(
                {
                    "model_id": outcome.model_id,
                    "model_revision_digest": _digest(outcome.model_revision),
                    "embedding_space_id_digest": _digest(outcome.embedding_space_id),
                    "dimension": outcome.dimension,
                    "normalized": outcome.normalized,
                }
            )
        else:
            payload.update(
                {
                    "error_code": outcome.code,
                    "recoverable": outcome.recoverable,
                    "model_id": outcome.model_id,
                    "model_revision_digest": _digest(outcome.model_revision),
                    "embedding_space_id_digest": _digest(outcome.embedding_space_id),
                }
            )
    if priority is not None:
        payload["priority"] = priority
    if consumer_id is not None:
        payload["consumer_id"] = consumer_id
    if consumer_count is not None:
        payload["consumer_count"] = max(0, consumer_count)
    if cache_hit is not None:
        payload["cache_hit"] = cache_hit
    return {key: value for key, value in payload.items() if value is not None}


def emit_embedding_observation(
    observer: EmbeddingObserver | None,
    event_name: str,
    **facts: Any,
) -> None:
    """Best-effort event emission; observation failures never affect inference."""

    if observer is None or event_name not in EMBEDDING_EVENT_NAMES:
        return
    try:
        observer.record(
            EmbeddingTraceEvent(
                event_name=event_name,
                payload=embedding_trace_payload(**facts),
            )
        )
    except Exception:
        pass


def _digest(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
