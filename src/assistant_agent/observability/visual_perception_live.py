"""Local live transport for prompt-safe visual-perception diagnostics."""

from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
from typing import Any

from assistant_agent.observability.visual_perception_report import (
    VisualPerceptionReport,
    build_visual_perception_report,
    parse_visual_perception_log,
    parse_visual_perception_line,
)


class VisualPerceptionLogFollower:
    def __init__(self, log_file: Path, *, session_digest: str | None) -> None:
        self.log_file = Path(log_file)
        self.session_digest = (
            parse_visual_perception_log((), session_digest=session_digest).session_digest
            if session_digest is not None
            else None
        )
        self._identity: tuple[int, int] | None = None
        self._offset = 0
        self._buffer = b""
        self._line_order = 0

    def poll(self) -> tuple[dict[str, Any], ...]:
        try:
            stat = self.log_file.stat()
        except FileNotFoundError:
            return ()
        identity = (stat.st_dev, stat.st_ino)
        if self._identity != identity or stat.st_size < self._offset:
            self._identity = identity
            self._offset = 0
            self._buffer = b""
        with self.log_file.open("rb") as stream:
            stream.seek(self._offset)
            appended = stream.read()
            self._offset = stream.tell()
        if not appended:
            return ()
        combined = self._buffer + appended
        lines = combined.split(b"\n")
        if combined.endswith(b"\n"):
            complete_lines = lines[:-1]
            self._buffer = b""
        else:
            complete_lines = lines[:-1]
            self._buffer = lines[-1]
        events: list[dict[str, Any]] = []
        for encoded_line in complete_lines:
            event = parse_visual_perception_line(
                encoded_line.decode("utf-8", errors="replace"),
                session_digest=self.session_digest,
                order=self._line_order,
            )
            self._line_order += 1
            if event is not None:
                events.append(event)
        return tuple(events)


class VisualPerceptionLiveFeed:
    def __init__(
        self,
        log_file: Path,
        *,
        session_digest: str | None,
        max_events: int = 50_000,
    ) -> None:
        if max_events <= 0:
            raise ValueError("live feed event limit must be positive")
        self.session_digest = (
            parse_visual_perception_log((), session_digest=session_digest).session_digest
            if session_digest is not None
            else None
        )
        self._active_session_digest = self.session_digest
        self.max_events = max_events
        self._follower = VisualPerceptionLogFollower(
            log_file,
            session_digest=self.session_digest,
        )
        self._events: list[dict[str, Any]] = []
        self._next_event_id = 1
        self._lock = Lock()

    def snapshot(self) -> VisualPerceptionReport:
        with self._lock:
            self._refresh_locked()
            active_digest = self._active_session_digest or ""
            return build_visual_perception_report(
                tuple(
                    event
                    for event in self._events
                    if event.get("session_id_digest") == active_digest
                ),
                session_digest=active_digest,
            )

    def events_after(self, event_id: int) -> tuple[dict[str, Any], ...]:
        if event_id < 0:
            raise ValueError("live feed cursor cannot be negative")
        with self._lock:
            self._refresh_locked()
            return tuple(
                event for event in self._events if event["order"] > event_id
            )

    def _refresh_locked(self) -> None:
        for event in self._follower.poll():
            projected = {**event, "order": self._next_event_id}
            self._next_event_id += 1
            self._events.append(projected)
            if self.session_digest is None:
                self._active_session_digest = projected["session_id_digest"]
        overflow = len(self._events) - self.max_events
        if overflow > 0:
            del self._events[:overflow]


def format_visual_perception_sse(event: dict[str, Any], *, event_id: int) -> bytes:
    if event_id <= 0:
        raise ValueError("SSE event id must be positive")
    data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return (
        f"id: {event_id}\n"
        "event: visual-perception\n"
        f"data: {data}\n\n"
    ).encode("utf-8")
