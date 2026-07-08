"""Semantic keyframe selection based on combined change scores."""

from __future__ import annotations

from dataclasses import dataclass

from assistant_agent.video_ai.types import KeyframeChangeMetrics, KeyframeDecision, VideoFrame


@dataclass(frozen=True)
class KeyframeSelectorConfig:
    """Keyframe selection policy."""

    threshold: float = 0.35
    min_interval_seconds: float = 0.5
    max_interval_seconds: float = 8.0
    pixel_weight: float = 1.0
    structural_weight: float = 1.0
    semantic_weight: float = 1.0
    object_weight: float = 1.0


class SemanticKeyframeSelector:
    """Choose keyframes from sampled candidates."""

    def __init__(self, config: KeyframeSelectorConfig | None = None) -> None:
        self.config = config or KeyframeSelectorConfig()

    def with_score(self, metrics: KeyframeChangeMetrics) -> KeyframeChangeMetrics:
        return KeyframeChangeMetrics(
            pixel_change_score=metrics.pixel_change_score,
            structural_change_score=metrics.structural_change_score,
            semantic_change_score=metrics.semantic_change_score,
            object_change_score=metrics.object_change_score,
            keyframe_score=self.score(metrics),
        )

    def score(self, metrics: KeyframeChangeMetrics) -> float:
        return (
            metrics.pixel_change_score * self.config.pixel_weight
            + metrics.structural_change_score * self.config.structural_weight
            + metrics.semantic_change_score * self.config.semantic_weight
            + metrics.object_change_score * self.config.object_weight
        )

    def force_due(self, timestamp_seconds: float, last_keyframe_at: float | None) -> bool:
        if last_keyframe_at is None:
            return True
        return timestamp_seconds - last_keyframe_at >= self.config.max_interval_seconds

    def select(
        self,
        frame: VideoFrame,
        metrics: KeyframeChangeMetrics,
        *,
        last_keyframe_at: float | None,
    ) -> KeyframeDecision:
        if last_keyframe_at is None:
            return KeyframeDecision(selected=True, reason="initial_keyframe", score=metrics.keyframe_score)

        elapsed = frame.timestamp_seconds - last_keyframe_at
        if elapsed < self.config.min_interval_seconds:
            return KeyframeDecision(selected=False, reason="min_interval", score=metrics.keyframe_score)
        if elapsed >= self.config.max_interval_seconds:
            return KeyframeDecision(selected=True, reason="max_interval", score=metrics.keyframe_score)
        if metrics.keyframe_score >= self.config.threshold:
            if metrics.semantic_change_score >= max(metrics.pixel_change_score, metrics.structural_change_score):
                reason = "semantic_change"
            elif metrics.object_change_score > 0:
                reason = "object_change"
            else:
                reason = "visual_change"
            return KeyframeDecision(selected=True, reason=reason, score=metrics.keyframe_score)
        return KeyframeDecision(selected=False, reason="below_threshold", score=metrics.keyframe_score)
