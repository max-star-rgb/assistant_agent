"""Semantic keyframe selection policies used during pipeline migration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from assistant_agent.media.embedding.comparator import EmbeddingComparator
from assistant_agent.media.embedding.models import EmbeddingEvent
from assistant_agent.media.video.types import KeyframeChangeMetrics, KeyframeDecision, VideoFrame


SemanticKeyframeReason = Literal[
    "initial",
    "semantic",
    "max_interval",
    "interactive",
    "below_threshold",
]


@dataclass(frozen=True)
class SemanticKeyframeConfig:
    """Pure semantic selection policy for embedded frames."""

    min_interval_seconds: float = 0.5
    max_interval_seconds: float = 10.0
    semantic_threshold: float = 0.18

    def __post_init__(self) -> None:
        if self.min_interval_seconds < 0:
            raise ValueError("keyframe min interval must be non-negative")
        if self.max_interval_seconds <= 0:
            raise ValueError("keyframe max interval must be positive")
        if self.min_interval_seconds > self.max_interval_seconds:
            raise ValueError("keyframe min interval must not exceed max interval")
        if not 0.0 <= self.semantic_threshold <= 1.0:
            raise ValueError("keyframe semantic threshold must be between 0 and 1")


@dataclass(frozen=True)
class SemanticKeyframeDecision:
    """Decision made solely from embedding change and timing policy."""

    selected: bool
    reason: SemanticKeyframeReason
    semantic_change: float = 0.0


@dataclass(frozen=True)
class KeyframeSelectorConfig:
    """Keyframe selection policy."""

    min_interval_seconds: float = 0.5
    max_interval_seconds: float = 10.0
    structural_threshold: float = 0.35
    semantic_threshold: float = 0.18
    combined_threshold: float = 0.25
    structural_weight: float = 0.40
    semantic_weight: float = 0.60

    @property
    def threshold(self) -> float:
        """Compatibility alias for callers migrating from one combined threshold."""

        return self.combined_threshold


class SemanticKeyframeSelector:
    """Choose keyframes from embeddings, with temporary legacy-call support."""

    def __init__(
        self,
        config: SemanticKeyframeConfig | KeyframeSelectorConfig | None = None,
    ) -> None:
        self.config = config or SemanticKeyframeConfig()
        self.comparator = EmbeddingComparator()
        self._last_event: EmbeddingEvent | None = None
        self._last_selected_at: float | None = None

    def with_score(self, metrics: KeyframeChangeMetrics) -> KeyframeChangeMetrics:
        if not isinstance(self.config, KeyframeSelectorConfig):
            raise TypeError("legacy keyframe metrics require KeyframeSelectorConfig")
        return KeyframeChangeMetrics(
            pixel_change_score=metrics.pixel_change_score,
            structural_change_score=metrics.structural_change_score,
            semantic_change_score=metrics.semantic_change_score,
            object_change_score=metrics.object_change_score,
            keyframe_score=self.score(metrics),
        )

    def score(self, metrics: KeyframeChangeMetrics) -> float:
        if not isinstance(self.config, KeyframeSelectorConfig):
            raise TypeError("legacy keyframe metrics require KeyframeSelectorConfig")
        return (
            metrics.structural_change_score * self.config.structural_weight
            + metrics.semantic_change_score * self.config.semantic_weight
        )

    def force_due(
        self,
        timestamp_seconds: float,
        last_keyframe_at: float | None | object = ...,
    ) -> bool:
        if last_keyframe_at is ...:
            if not isinstance(self.config, SemanticKeyframeConfig):
                raise TypeError("semantic force_due requires SemanticKeyframeConfig")
            if self._last_selected_at is None:
                return True
            return (
                timestamp_seconds - self._last_selected_at
                >= self.config.max_interval_seconds
            )
        if not isinstance(self.config, KeyframeSelectorConfig):
            raise TypeError("legacy force_due requires KeyframeSelectorConfig")
        if last_keyframe_at is None:
            return True
        return timestamp_seconds - last_keyframe_at >= self.config.max_interval_seconds

    def select(
        self,
        frame: EmbeddingEvent | VideoFrame,
        metrics: KeyframeChangeMetrics | None = None,
        *,
        last_keyframe_at: float | None = None,
        frame_timestamp_seconds: float | None = None,
        force_interactive: bool = False,
    ) -> SemanticKeyframeDecision | KeyframeDecision:
        if isinstance(frame, EmbeddingEvent):
            if not isinstance(self.config, SemanticKeyframeConfig):
                raise TypeError("embedding selection requires SemanticKeyframeConfig")
            if frame_timestamp_seconds is None:
                raise TypeError("frame_timestamp_seconds is required")
            return self._select_embedding(
                frame,
                frame_timestamp_seconds=frame_timestamp_seconds,
                force_interactive=force_interactive,
            )
        if not isinstance(self.config, KeyframeSelectorConfig) or metrics is None:
            raise TypeError("legacy selection requires KeyframeSelectorConfig and metrics")
        if last_keyframe_at is None:
            return KeyframeDecision(selected=True, reason="initial_keyframe", score=metrics.keyframe_score)

        elapsed = frame.timestamp_seconds - last_keyframe_at
        if elapsed < self.config.min_interval_seconds:
            return KeyframeDecision(selected=False, reason="min_interval", score=metrics.keyframe_score)
        if elapsed >= self.config.max_interval_seconds:
            return KeyframeDecision(selected=True, reason="max_interval", score=metrics.keyframe_score)
        structural_triggered = (
            metrics.structural_change_score >= self.config.structural_threshold
        )
        semantic_triggered = (
            metrics.semantic_change_score >= self.config.semantic_threshold
        )
        combined_triggered = (
            metrics.structural_change_score > 0.0
            and metrics.semantic_change_score > 0.0
            and metrics.keyframe_score >= self.config.combined_threshold
        )
        if combined_triggered:
            reason = (
                "structural_and_semantic_change"
                if structural_triggered and semantic_triggered
                else "combined_change"
            )
            return KeyframeDecision(selected=True, reason=reason, score=metrics.keyframe_score)
        if structural_triggered and semantic_triggered:
            return KeyframeDecision(
                selected=True,
                reason="structural_and_semantic_change",
                score=metrics.keyframe_score,
            )
        if structural_triggered:
            return KeyframeDecision(
                selected=True,
                reason="structural_change",
                score=metrics.keyframe_score,
            )
        if semantic_triggered:
            return KeyframeDecision(
                selected=True,
                reason="semantic_change",
                score=metrics.keyframe_score,
            )
        return KeyframeDecision(selected=False, reason="below_threshold", score=metrics.keyframe_score)

    def _select_embedding(
        self,
        event: EmbeddingEvent,
        *,
        frame_timestamp_seconds: float,
        force_interactive: bool,
    ) -> SemanticKeyframeDecision:
        assert isinstance(self.config, SemanticKeyframeConfig)
        if force_interactive:
            return self._commit_embedding(event, frame_timestamp_seconds, "interactive")
        if self._last_event is None or self._last_selected_at is None:
            return self._commit_embedding(event, frame_timestamp_seconds, "initial")

        elapsed = frame_timestamp_seconds - self._last_selected_at
        if elapsed >= self.config.max_interval_seconds:
            return self._commit_embedding(event, frame_timestamp_seconds, "max_interval")

        semantic_change = 1.0 - self.comparator.similarity(event, self._last_event)
        if (
            elapsed >= self.config.min_interval_seconds
            and semantic_change >= self.config.semantic_threshold
        ):
            return self._commit_embedding(
                event,
                frame_timestamp_seconds,
                "semantic",
                semantic_change=semantic_change,
            )
        return SemanticKeyframeDecision(
            selected=False,
            reason="below_threshold",
            semantic_change=semantic_change,
        )

    def _commit_embedding(
        self,
        event: EmbeddingEvent,
        frame_timestamp_seconds: float,
        reason: SemanticKeyframeReason,
        *,
        semantic_change: float = 0.0,
    ) -> SemanticKeyframeDecision:
        self._last_event = event
        self._last_selected_at = frame_timestamp_seconds
        return SemanticKeyframeDecision(
            selected=True,
            reason=reason,
            semantic_change=semantic_change,
        )
