"""Bounded prompt-safe semantic memory for realtime video streams."""

from __future__ import annotations

from threading import Lock
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from assistant_agent.schemas.context import RealtimeVideoContext, RealtimeVideoContextStatus
from assistant_agent.schemas.perception import VideoUnderstandingResult


class SemanticKeyframeRecord(BaseModel):
    """One retained keyframe reference associated with a semantic update."""

    model_config = ConfigDict(frozen=True)

    frame_id: str
    uri: str
    sequence: int
    timestamp_ms: int | None = None


class RealtimeVideoObservationDiagnostics(BaseModel):
    """Prompt-safe timing for the latest successful rolling observation."""

    model_config = ConfigDict(frozen=True)

    h264_decode_latency_ms: int | None = Field(default=None, ge=0)
    keyframe_selection_latency_ms: int | None = Field(default=None, ge=0)
    queue_wait_latency_ms: int | None = Field(default=None, ge=0)
    observation_latency_ms: int | None = Field(default=None, ge=0)
    published_at_ms: int | None = Field(default=None, ge=0)
    transport: str | None = Field(default=None, max_length=40)
    session_generation: int | None = Field(default=None, ge=1)
    connection_reused: bool | None = None
    reconnect_count: int | None = Field(default=None, ge=0)
    target_sequence: int | None = Field(default=None, ge=0)
    completed_sequence: int | None = Field(default=None, ge=0)
    first_delta_latency_ms: int | None = Field(default=None, ge=0)
    total_observation_latency_ms: int | None = Field(default=None, ge=0)


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
    observation_diagnostics: RealtimeVideoObservationDiagnostics | None = None

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
        *,
        diagnostics: RealtimeVideoObservationDiagnostics | None = None,
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
                    "observation_diagnostics": diagnostics,
                },
                deep=True,
            )
            return list(evicted)

    def record_failure(
        self,
        video_id: str,
        frame: SemanticKeyframeRecord,
        error: dict[str, Any],
        *,
        diagnostics: RealtimeVideoObservationDiagnostics | None = None,
    ) -> None:
        """Record a completed failed observation without erasing prior state."""

        _ = frame
        with self._lock:
            current = self._snapshots.get(video_id) or RealtimeVideoSnapshot(video_id=video_id)
            update: dict[str, Any] = {
                "last_observation_status": "failed",
                "last_error": dict(error),
                "in_flight": False,
            }
            if diagnostics is not None:
                successful_diagnostics = current.observation_diagnostics
                if (
                    diagnostics.published_at_ms is None
                    and successful_diagnostics is not None
                    and successful_diagnostics.published_at_ms is not None
                ):
                    diagnostics = diagnostics.model_copy(
                        update={"published_at_ms": successful_diagnostics.published_at_ms}
                    )
                update["observation_diagnostics"] = diagnostics
            self._snapshots[video_id] = current.model_copy(
                update=update,
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


def project_realtime_video_context(
    snapshot: RealtimeVideoSnapshot | None,
    *,
    now_ms: int,
    target_sequence: int | None = None,
) -> RealtimeVideoContext:
    """Project internal rolling state without paths, raw errors, or media data."""

    if snapshot is None:
        return RealtimeVideoContext(
            target_sequence=target_sequence,
            sequence_gap=target_sequence,
        )
    has_success = snapshot.last_success_sequence is not None
    pending = snapshot.in_flight or snapshot.pending_count > 0
    sequence_gap = (
        max(0, target_sequence - (snapshot.last_success_sequence or 0))
        if target_sequence is not None
        else None
    )
    if has_success and pending:
        status: RealtimeVideoContextStatus = "refreshing"
    elif has_success and sequence_gap:
        status = "stale"
    elif has_success and snapshot.last_observation_status == "failed":
        status = "stale"
    elif has_success:
        status = "ready"
    elif pending:
        status = "pending"
    elif snapshot.last_observation_status == "failed":
        status = "failed"
    else:
        status = "unavailable"
    diagnostics = snapshot.observation_diagnostics
    published_at_ms = diagnostics.published_at_ms if diagnostics is not None else None
    capture_age = _past_age_ms(snapshot.last_success_timestamp_ms, now_ms)
    publish_age = _past_age_ms(published_at_ms, now_ms)
    error_code = snapshot.last_error.get("code") if isinstance(snapshot.last_error, dict) else None
    return RealtimeVideoContext(
        status=status,
        summary=_clip_text(snapshot.current_state, 400),
        objects=_clip_items(snapshot.objects, limit=4),
        people=_clip_items(snapshot.people, limit=2),
        actions=_clip_items(snapshot.actions, limit=3),
        events=_clip_items(snapshot.events, limit=3),
        scene=_clip_text(snapshot.scene or "", 100) or None,
        snapshot_sequence=snapshot.last_success_sequence,
        target_sequence=target_sequence,
        sequence_gap=sequence_gap,
        snapshot_age_ms=capture_age if capture_age is not None else publish_age,
        frame_capture_age_ms=capture_age,
        snapshot_publish_age_ms=publish_age,
        observation_latency_ms=(diagnostics.observation_latency_ms if diagnostics is not None else None),
        provider=_clip_text(snapshot.provider or "", 80) or None,
        model=_clip_text(snapshot.model or "", 120) or None,
        pending_count=snapshot.pending_count,
        in_flight=snapshot.in_flight,
        error_code=_clip_text(str(error_code), 80) if error_code else None,
        transport=diagnostics.transport if diagnostics is not None else None,
        session_generation=(diagnostics.session_generation if diagnostics is not None else None),
        connection_reused=(diagnostics.connection_reused if diagnostics is not None else None),
        reconnect_count=(diagnostics.reconnect_count if diagnostics is not None else None),
        completed_sequence=(diagnostics.completed_sequence if diagnostics is not None else None),
        first_delta_latency_ms=(
            diagnostics.first_delta_latency_ms if diagnostics is not None else None
        ),
        total_observation_latency_ms=(
            diagnostics.total_observation_latency_ms if diagnostics is not None else None
        ),
    )


def _past_age_ms(value: int | None, now_ms: int) -> int | None:
    if value is None or value > now_ms:
        return None
    return now_ms - value


def _clip_items(values: list[str], *, limit: int) -> list[str]:
    return [_clip_text(value, 60) for value in values[:limit] if _clip_text(value, 60)]


def _clip_text(value: str, max_chars: int) -> str:
    normalized = str(value).strip()
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 3].rstrip() + "..."
