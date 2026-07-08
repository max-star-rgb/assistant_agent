"""Shared models for adaptive realtime video understanding."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class VideoFrame:
    """One frame from a realtime video stream.

    ``pixels`` is intentionally permissive so tests and optional camera
    adapters can pass nested lists, bytes, or ndarray-like objects without
    adding hard image-processing dependencies to the default install.
    """

    frame_id: str
    timestamp_seconds: float
    pixels: Any | None = None
    uri: str | None = None
    width: int | None = None
    height: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class KeyframeChangeMetrics:
    """Scores used by the semantic keyframe selector."""

    pixel_change_score: float = 0.0
    structural_change_score: float = 0.0
    semantic_change_score: float = 0.0
    object_change_score: float = 0.0
    keyframe_score: float = 0.0

    @property
    def change_score(self) -> float:
        """Return the strongest single signal for adaptive sampling."""

        return max(
            self.pixel_change_score,
            self.structural_change_score,
            self.semantic_change_score,
            self.object_change_score,
        )


@dataclass(frozen=True)
class SamplingDecision:
    """Adaptive sampler decision for one frame."""

    sampled: bool
    sampling_rate: float
    reason: str


@dataclass(frozen=True)
class KeyframeDecision:
    """Keyframe selector decision for one sampled frame."""

    selected: bool
    reason: str
    score: float


@dataclass(frozen=True)
class FrameProcessingResult:
    """Public result returned after processing a frame."""

    frame_id: str
    timestamp_seconds: float
    sampled: bool
    sampling_rate: float
    metrics: KeyframeChangeMetrics
    keyframe_selected: bool
    qwen_called: bool
    latency_ms: int
    decision_reason: str
    errors: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class QueryAnswer:
    """Answer returned from rolling video memory."""

    answer: str
    memory_state: dict[str, Any]
    qwen_called: bool
    latency_ms: int
