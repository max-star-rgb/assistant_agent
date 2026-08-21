"""Pure embedding-based semantic keyframe selection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from assistant_agent.media.embedding.comparator import EmbeddingComparator
from assistant_agent.media.embedding.models import EmbeddingEvent


SemanticKeyframeReason = Literal[
    "initial",
    "semantic",
    "max_interval",
    "interactive",
    "below_threshold",
]


@dataclass(frozen=True)
class SemanticKeyframeConfig:
    max_interval_seconds: float = 2.0
    semantic_threshold: float = 0.08

    def __post_init__(self) -> None:
        if self.max_interval_seconds <= 0:
            raise ValueError("keyframe max interval must be positive")
        if not 0.0 <= self.semantic_threshold <= 1.0:
            raise ValueError("keyframe semantic threshold must be between 0 and 1")


@dataclass(frozen=True)
class SemanticKeyframeDecision:
    selected: bool
    reason: SemanticKeyframeReason
    semantic_change: float = 0.0
    semantic_similarity: float | None = None
    reference_sequence: int | None = None


class SemanticKeyframeSelector:
    def __init__(self, config: SemanticKeyframeConfig | None = None) -> None:
        self.config = config or SemanticKeyframeConfig()
        self.comparator = EmbeddingComparator()
        self._last_event: EmbeddingEvent | None = None
        self._last_selected_at: float | None = None

    def force_due(self, frame_timestamp_seconds: float) -> bool:
        return self._last_selected_at is None or (
            frame_timestamp_seconds - self._last_selected_at
            >= self.config.max_interval_seconds
        )

    def select(
        self,
        event: EmbeddingEvent,
        *,
        frame_timestamp_seconds: float,
        force_interactive: bool = False,
    ) -> SemanticKeyframeDecision:
        if force_interactive:
            return self._commit(event, frame_timestamp_seconds, "interactive")
        if self._last_event is None or self._last_selected_at is None:
            return self._commit(event, frame_timestamp_seconds, "initial")
        elapsed = frame_timestamp_seconds - self._last_selected_at
        if elapsed >= self.config.max_interval_seconds:
            return self._commit(event, frame_timestamp_seconds, "max_interval")
        reference_sequence = self._last_event.frame_sequence
        semantic_similarity = self.comparator.similarity(event, self._last_event)
        semantic_change = 1.0 - semantic_similarity
        if semantic_change >= self.config.semantic_threshold:
            return self._commit(
                event,
                frame_timestamp_seconds,
                "semantic",
                semantic_change=semantic_change,
                semantic_similarity=semantic_similarity,
                reference_sequence=reference_sequence,
            )
        return SemanticKeyframeDecision(
            selected=False,
            reason="below_threshold",
            semantic_change=semantic_change,
            semantic_similarity=semantic_similarity,
            reference_sequence=reference_sequence,
        )

    def _commit(
        self,
        event: EmbeddingEvent,
        frame_timestamp_seconds: float,
        reason: SemanticKeyframeReason,
        *,
        semantic_change: float = 0.0,
        semantic_similarity: float | None = None,
        reference_sequence: int | None = None,
    ) -> SemanticKeyframeDecision:
        if reference_sequence is None and self._last_event is not None:
            reference_sequence = self._last_event.frame_sequence
        self._last_event = event
        self._last_selected_at = frame_timestamp_seconds
        return SemanticKeyframeDecision(
            selected=True,
            reason=reason,
            semantic_change=semantic_change,
            semantic_similarity=semantic_similarity,
            reference_sequence=reference_sequence,
        )
