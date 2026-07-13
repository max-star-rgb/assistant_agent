"""Bounded prompt-safe semantic memory for realtime video streams."""

from __future__ import annotations

from threading import Lock
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from assistant_agent.schemas.perception import VideoUnderstandingResult


class SemanticKeyframeRecord(BaseModel):
    """One retained keyframe reference associated with a semantic update."""

    model_config = ConfigDict(frozen=True)

    frame_id: str
    uri: str
    sequence: int
    timestamp_ms: int | None = None


class RealtimeVideoSnapshot(BaseModel):
    """Immutable semantic snapshot exposed to the video understanding tool."""

    model_config = ConfigDict(frozen=True)

    video_id: str
    current_state: str = ""
    objects: list[str] = Field(default_factory=list)
    people: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    events: list[str] = Field(default_factory=list)
    scene: str | None = None
    products: list[str] = Field(default_factory=list)
    brands: list[str] = Field(default_factory=list)
    colors: list[str] = Field(default_factory=list)
    materials: list[str] = Field(default_factory=list)
    text_in_video: list[str] = Field(default_factory=list)
    timestamps: list[dict[str, Any]] = Field(default_factory=list)
    style_tags: list[str] = Field(default_factory=list)
    confidence: float | None = None
    provider: str | None = None
    model: str | None = None
    keyframes: list[SemanticKeyframeRecord] = Field(default_factory=list)
    last_success_sequence: int | None = None
    last_success_timestamp_ms: int | None = None
    last_observation_status: Literal["succeeded", "failed"] | None = None
    last_error: dict[str, Any] | None = None
    pending_count: int = 0
    in_flight: bool = False

    @property
    def healthy(self) -> bool:
        return self.last_success_sequence is not None and self.last_observation_status == "succeeded"


class RealtimeVideoMemoryStore:
    """Thread-safe rolling semantic snapshots keyed by opaque video id."""

    def __init__(self, *, max_keyframes: int = 8, max_events: int = 50) -> None:
        if max_keyframes <= 0:
            raise ValueError("max_keyframes must be positive")
        if max_events <= 0:
            raise ValueError("max_events must be positive")
        self.max_keyframes = max_keyframes
        self.max_events = max_events
        self._snapshots: dict[str, RealtimeVideoSnapshot] = {}
        self._lock = Lock()

    def record_success(
        self,
        video_id: str,
        frame: SemanticKeyframeRecord,
        result: VideoUnderstandingResult,
    ) -> list[SemanticKeyframeRecord]:
        """Apply one successful observation and return evicted keyframes."""

        with self._lock:
            current = self._snapshots.get(video_id) or RealtimeVideoSnapshot(video_id=video_id)
            all_keyframes = [*current.keyframes, frame]
            evicted = all_keyframes[: max(0, len(all_keyframes) - self.max_keyframes)]
            retained = all_keyframes[-self.max_keyframes :]
            events = _bounded_unique([*current.events, *result.events], self.max_events)
            self._snapshots[video_id] = current.model_copy(
                update={
                    "current_state": result.summary,
                    "objects": list(result.objects),
                    "people": list(getattr(result, "people", [])),
                    "actions": list(result.actions),
                    "events": events,
                    "scene": result.scene,
                    "products": list(result.products),
                    "brands": list(result.brands),
                    "colors": list(result.colors),
                    "materials": list(result.materials),
                    "text_in_video": list(result.text_in_video),
                    "timestamps": [dict(item) for item in result.timestamps],
                    "style_tags": list(result.style_tags),
                    "confidence": result.confidence,
                    "provider": result.provider,
                    "model": result.model,
                    "keyframes": retained,
                    "last_success_sequence": frame.sequence,
                    "last_success_timestamp_ms": frame.timestamp_ms,
                    "last_observation_status": "succeeded",
                    "last_error": None,
                    "in_flight": False,
                },
                deep=True,
            )
            return list(evicted)

    def record_failure(
        self,
        video_id: str,
        frame: SemanticKeyframeRecord,
        error: dict[str, Any],
    ) -> None:
        """Record a completed failed observation without erasing prior state."""

        _ = frame
        with self._lock:
            current = self._snapshots.get(video_id) or RealtimeVideoSnapshot(video_id=video_id)
            self._snapshots[video_id] = current.model_copy(
                update={
                    "last_observation_status": "failed",
                    "last_error": dict(error),
                    "in_flight": False,
                },
                deep=True,
            )

    def mark_pending(self, video_id: str, *, pending_count: int, in_flight: bool) -> None:
        """Update bounded queue state without changing observation health."""

        with self._lock:
            current = self._snapshots.get(video_id) or RealtimeVideoSnapshot(video_id=video_id)
            self._snapshots[video_id] = current.model_copy(
                update={
                    "pending_count": max(0, pending_count),
                    "in_flight": bool(in_flight),
                },
                deep=True,
            )

    def snapshot(self, video_id: str) -> RealtimeVideoSnapshot | None:
        """Return a deep immutable snapshot for one video id."""

        with self._lock:
            snapshot = self._snapshots.get(video_id)
            return snapshot.model_copy(deep=True) if snapshot is not None else None

    def remove_video(self, video_id: str) -> RealtimeVideoSnapshot | None:
        """Remove and return one video's semantic state."""

        with self._lock:
            snapshot = self._snapshots.pop(video_id, None)
            return snapshot.model_copy(deep=True) if snapshot is not None else None


def _bounded_unique(values: list[str], limit: int) -> list[str]:
    retained: list[str] = []
    for value in values:
        normalized = str(value).strip()
        if normalized and normalized not in retained:
            retained.append(normalized)
    return retained[-limit:]
