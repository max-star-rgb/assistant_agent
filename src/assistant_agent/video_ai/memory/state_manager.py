"""Rolling memory state for realtime video understanding."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from assistant_agent.video_ai.types import VideoFrame


@dataclass(frozen=True)
class VideoEvent:
    """One event extracted from keyframe understanding."""

    time: str
    event: str
    frame_id: str
    timestamp_seconds: float


@dataclass(frozen=True)
class KeyframeMemoryRecord:
    """Prompt-safe keyframe reference stored in rolling memory."""

    frame_id: str
    timestamp_seconds: float
    uri: str | None
    summary: str
    scene: str
    objects: list[str]
    people: list[str]


@dataclass
class VideoMemoryStateManager:
    """Maintain current state, recent events, timeline, and keyframe refs."""

    max_events: int = 50
    max_keyframes: int = 8
    current_state: str = ""
    events: list[VideoEvent] = field(default_factory=list)
    timeline: list[VideoEvent] = field(default_factory=list)
    keyframes: list[KeyframeMemoryRecord] = field(default_factory=list)

    def apply_observation(self, frame: VideoFrame, observation: Any) -> None:
        summary = str(getattr(observation, "summary", "") or "").strip()
        if summary:
            self.current_state = summary
        scene = str(getattr(observation, "scene", "") or "")
        objects = _string_list(getattr(observation, "objects", []))
        people = _string_list(getattr(observation, "people", []))

        events = _string_list(getattr(observation, "important_events", []))
        changes = str(getattr(observation, "changes_from_previous", "") or "").strip()
        if changes and not events:
            events = [changes]
        for event_text in events:
            event = VideoEvent(
                time=format_video_time(frame.timestamp_seconds),
                event=event_text,
                frame_id=frame.frame_id,
                timestamp_seconds=frame.timestamp_seconds,
            )
            self.events.append(event)
            self.timeline.append(event)

        self.events = self.events[-self.max_events :]
        self.timeline = self.timeline[-self.max_events :]
        self.keyframes.append(
            KeyframeMemoryRecord(
                frame_id=frame.frame_id,
                timestamp_seconds=frame.timestamp_seconds,
                uri=frame.uri,
                summary=summary,
                scene=scene,
                objects=objects,
                people=people,
            )
        )
        self.keyframes = self.keyframes[-self.max_keyframes :]

    def recent_keyframes(self, *, limit: int | None = None) -> list[KeyframeMemoryRecord]:
        selected_limit = limit or self.max_keyframes
        return self.keyframes[-selected_limit:]

    def snapshot(self) -> dict[str, Any]:
        return {
            "current_state": self.current_state,
            "events": [event.__dict__.copy() for event in self.events],
            "timeline": [event.__dict__.copy() for event in self.timeline],
            "keyframes": [record.__dict__.copy() for record in self.keyframes],
        }


def format_video_time(timestamp_seconds: float) -> str:
    total_ms = max(0, int(round(timestamp_seconds * 1000)))
    millis = total_ms % 1000
    total_seconds = total_ms // 1000
    seconds = total_seconds % 60
    minutes = (total_seconds // 60) % 60
    hours = total_seconds // 3600
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
