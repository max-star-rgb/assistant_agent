"""Prompt-safe, content-free observability for multimodal embedding lifecycle."""

from __future__ import annotations

import hashlib
import math
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
    "embedding.session_cleanup",
)

SEMANTIC_FRAME_EVENT_NAMES = (
    "semantic_frame.admitted",
    "semantic_frame.skipped",
    "semantic_frame.replaced",
    "semantic_frame.selected",
)
VISUAL_REMINDER_EVENT_NAMES = (
    "visual_reminder.created",
    "visual_reminder.compared",
    "visual_reminder.triggered",
    "visual_reminder.cancelled",
)
VISUAL_SEMANTIC_EVENT_NAMES = (
    "visual_semantic.retained",
    "visual_semantic.evicted",
    "visual_semantic.index_failed",
    "visual_memory.query",
    "visual_memory.compaction",
)
SEMANTIC_FRAME_REASONS = frozenset(
    {
        "admitted",
        "below_threshold",
        "embedding_failed",
        "initial",
        "interactive",
        "interactive_inflight",
        "interactive_pending",
        "latest_wins",
        "max_interval",
        "non_increasing",
        "processing_error",
        "semantic",
        "superseded_before_admission",
    }
)


class EmbeddingTraceEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_name: str = Field(pattern=r"^embedding\.")
    payload: dict[str, Any] = Field(default_factory=dict)


class SemanticFrameTraceEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_name: str = Field(pattern=r"^semantic_frame\.")
    payload: dict[str, Any] = Field(default_factory=dict)


class VisualSemanticTraceEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_name: str = Field(pattern=r"^(visual_semantic|visual_memory)\.")
    payload: dict[str, Any] = Field(default_factory=dict)


class VisualReminderTraceEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_name: str = Field(pattern=r"^visual_reminder\.")
    payload: dict[str, Any] = Field(default_factory=dict)


TraceEvent = (
    EmbeddingTraceEvent
    | SemanticFrameTraceEvent
    | VisualReminderTraceEvent
    | VisualSemanticTraceEvent
)


class EmbeddingObserver(Protocol):
    def record(self, event: TraceEvent) -> None: ...


class InMemoryEmbeddingObserver:
    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    def record(self, event: TraceEvent) -> None:
        self.events.append(event.model_copy(deep=True))


def embedding_trace_payload(
    *,
    outcome: EmbeddingEvent | EmbeddingFailureEvent | None = None,
    observation: ImageObservation | TextObservation | None = None,
    priority: str | None = None,
    cache_hit: bool | None = None,
) -> dict[str, Any]:
    """Project lifecycle facts without vectors, text, paths, or raw identities."""

    payload: dict[str, Any] = {}
    if observation is not None:
        payload.update(
            {
                "modality": "image"
                if isinstance(observation, ImageObservation)
                else "text",
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


def semantic_frame_trace_payload(
    *,
    session_id: str,
    sequence: int,
    reason: str | None = None,
    replaced_sequence: int | None = None,
    reference_sequence: int | None = None,
    semantic_similarity: float | None = None,
    semantic_change: float | None = None,
    semantic_threshold: float | None = None,
    selected: bool | None = None,
    **_content: Any,
) -> dict[str, Any]:
    """Project scheduling facts without frame content or raw identities."""

    payload: dict[str, Any] = {
        "session_id_digest": _digest(session_id),
        "sequence": sequence,
    }
    if reason is not None:
        payload["reason"] = reason if reason in SEMANTIC_FRAME_REASONS else "other"
    if replaced_sequence is not None:
        payload["replaced_sequence"] = replaced_sequence
    if reference_sequence is not None:
        payload["reference_sequence"] = reference_sequence
    if semantic_similarity is not None and math.isfinite(semantic_similarity):
        payload["semantic_similarity"] = max(-1.0, min(1.0, semantic_similarity))
    if semantic_change is not None and math.isfinite(semantic_change):
        payload["semantic_change"] = max(0.0, min(2.0, semantic_change))
    if semantic_threshold is not None and math.isfinite(semantic_threshold):
        payload["semantic_threshold"] = max(0.0, min(1.0, semantic_threshold))
    if selected is not None:
        payload["selected"] = selected
    return payload


def emit_semantic_frame_observation(
    observer: EmbeddingObserver | None,
    event_name: str,
    **facts: Any,
) -> None:
    """Best-effort semantic scheduling event with content-safe projection."""

    if observer is None or event_name not in SEMANTIC_FRAME_EVENT_NAMES:
        return
    try:
        observer.record(
            SemanticFrameTraceEvent(
                event_name=event_name,
                payload=semantic_frame_trace_payload(**facts),
            )
        )
    except Exception:
        pass


def visual_reminder_trace_payload(
    *,
    session_id: str,
    reminder_id: str,
    frame_sequence: int | None = None,
    similarity: float | None = None,
    similarity_threshold: float | None = None,
    matched: bool | None = None,
    status: str | None = None,
    **_content: Any,
) -> dict[str, Any]:
    """Project reminder diagnostics without target text, vectors, or media content."""

    payload: dict[str, Any] = {
        "session_id_digest": _digest(session_id),
        "reminder_id": reminder_id,
    }
    if frame_sequence is not None:
        payload["frame_sequence"] = frame_sequence
    if similarity is not None and math.isfinite(similarity):
        payload["similarity"] = max(-1.0, min(1.0, similarity))
    if similarity_threshold is not None and math.isfinite(similarity_threshold):
        payload["similarity_threshold"] = max(
            0.0, min(1.0, similarity_threshold)
        )
    if matched is not None:
        payload["matched"] = matched
    if status is not None:
        payload["status"] = status
    return payload


def emit_visual_reminder_observation(
    observer: EmbeddingObserver | None,
    event_name: str,
    **facts: Any,
) -> None:
    """Best-effort content-free reminder event for local diagnostics."""

    if observer is None or event_name not in VISUAL_REMINDER_EVENT_NAMES:
        return
    try:
        observer.record(
            VisualReminderTraceEvent(
                event_name=event_name,
                payload=visual_reminder_trace_payload(**facts),
            )
        )
    except Exception:
        pass


def visual_semantic_trace_payload(
    *,
    session_id: str,
    sequence: int | None = None,
    status: str | None = None,
    count: int | None = None,
    returned_count: int | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    target_tokens: int | None = None,
    attempts: int | None = None,
    latency_ms: int | None = None,
    triggered: bool | None = None,
    hard: bool | None = None,
    target_reached: bool | None = None,
    **_content: Any,
) -> dict[str, Any]:
    """Project record/query facts without visual or query content."""

    payload: dict[str, Any] = {"session_id_digest": _digest(session_id)}
    if sequence is not None:
        payload["sequence"] = max(0, sequence)
    if status is not None:
        payload["status"] = (
            status
            if status
            in {
                "candidate",
                "confirmed",
                "empty",
                "not_found",
                "records",
                "ready",
                "not_needed",
                "succeeded",
                "failed_below_hard",
                "hard_limit",
                "unavailable",
            }
            else "other"
        )
    if count is not None:
        payload["count"] = max(0, count)
    integer_facts = {
        "returned_count": returned_count,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "target_tokens": target_tokens,
        "attempts": attempts,
    }
    payload.update(
        {
            key: max(0, value)
            for key, value in integer_facts.items()
            if value is not None
        }
    )
    if latency_ms is not None:
        payload["latency_ms"] = max(0, latency_ms)
    boolean_facts = {
        "triggered": triggered,
        "hard": hard,
        "target_reached": target_reached,
    }
    payload.update(
        {
            key: value
            for key, value in boolean_facts.items()
            if isinstance(value, bool)
        }
    )
    return payload


def emit_visual_semantic_observation(
    observer: EmbeddingObserver | None,
    event_name: str,
    **facts: Any,
) -> None:
    if observer is None or event_name not in VISUAL_SEMANTIC_EVENT_NAMES:
        return
    try:
        observer.record(
            VisualSemanticTraceEvent(
                event_name=event_name,
                payload=visual_semantic_trace_payload(**facts),
            )
        )
    except Exception:
        pass


def _digest(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
