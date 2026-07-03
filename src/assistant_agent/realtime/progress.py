"""Progress event throttling and heartbeat policy for realtime runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic
from typing import Callable

from assistant_agent.realtime.types import RealtimeAgentEvent


NowFn = Callable[[], float]


@dataclass(frozen=True)
class ProgressPolicy:
    """Per-run policy for user-visible progress updates."""

    min_interval_s: float = 1.0
    heartbeat_interval_s: float = 30.0
    significant_progress_delta: float = 0.1

    def tracker(self, *, now_fn: NowFn = monotonic) -> ProgressTracker:
        return ProgressTracker(policy=self, now_fn=now_fn)


@dataclass
class _ProgressRecord:
    emitted_at: float
    progress: float | None = None


@dataclass
class ProgressTracker:
    """Stateful progress policy for one realtime run."""

    policy: ProgressPolicy = field(default_factory=ProgressPolicy)
    now_fn: NowFn = monotonic
    _last_emitted_at: float | None = None
    _last_progress_event: RealtimeAgentEvent | None = None
    _records: dict[tuple[str, str, str, str], _ProgressRecord] = field(default_factory=dict)

    def should_emit(self, event: RealtimeAgentEvent) -> bool:
        """Return whether the event should be forwarded to the user."""

        if event.type != "run.progress":
            self._record_emit(event)
            return True

        self._last_progress_event = event
        if self.policy.min_interval_s <= 0:
            self._record_emit(event)
            return True

        now = self.now_fn()
        key = _progress_signature(event)
        progress = _progress_value(event)
        record = self._records.get(key)
        if record is not None:
            elapsed = now - record.emitted_at
            if elapsed < self.policy.min_interval_s and not _progress_delta_reached(
                progress,
                record.progress,
                self.policy.significant_progress_delta,
            ):
                return False

        self._record_emit(event, now=now)
        return True

    def heartbeat(self) -> RealtimeAgentEvent | None:
        """Return a heartbeat progress event when the user has seen no recent output."""

        if self.policy.heartbeat_interval_s <= 0:
            return None
        if self._last_progress_event is None or self._last_emitted_at is None:
            return None

        now = self.now_fn()
        elapsed = now - self._last_emitted_at
        if elapsed < self.policy.heartbeat_interval_s:
            return None

        payload = dict(self._last_progress_event.payload)
        payload["heartbeat"] = True
        payload["display_only"] = True
        payload["elapsed_since_update_s"] = round(elapsed, 3)
        payload.setdefault("status", "working")
        payload.setdefault("stage", "runtime")
        payload["message"] = _heartbeat_message(payload, self._last_progress_event.text)

        event = RealtimeAgentEvent(
            type="run.progress",
            text=payload["message"],
            payload=payload,
            display_only=True,
        )
        self._record_emit(event, now=now)
        return event

    def heartbeat_poll_interval_s(self) -> float:
        """Return a bounded polling interval for the heartbeat loop."""

        interval = self.policy.heartbeat_interval_s
        if interval <= 0:
            return 1.0
        return min(max(interval / 2, 0.05), 1.0)

    def _record_emit(self, event: RealtimeAgentEvent, *, now: float | None = None) -> None:
        now = self.now_fn() if now is None else now
        self._last_emitted_at = now
        if event.type != "run.progress":
            return
        self._last_progress_event = event
        self._records[_progress_signature(event)] = _ProgressRecord(
            emitted_at=now,
            progress=_progress_value(event),
        )


def _progress_signature(event: RealtimeAgentEvent) -> tuple[str, str, str, str]:
    payload = event.payload
    stage = _payload_text(payload, "stage")
    status = _payload_text(payload, "status")
    step = (
        _payload_text(payload, "current_step")
        or _payload_text(payload, "tool_name")
        or _payload_text(payload, "node_name")
    )
    message = _payload_text(payload, "message") or event.text or ""
    return (stage, status, step, message)


def _progress_value(event: RealtimeAgentEvent) -> float | None:
    value = event.payload.get("progress")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _progress_delta_reached(
    current: float | None,
    previous: float | None,
    delta: float,
) -> bool:
    if current is None or previous is None:
        return False
    return abs(current - previous) >= delta


def _payload_text(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) else ""


def _heartbeat_message(payload: dict[str, object], fallback: str | None) -> str:
    step = _payload_text(payload, "current_step") or _payload_text(payload, "tool_name")
    if step:
        return f"Still working on {step}."
    stage = _payload_text(payload, "stage")
    if stage == "tool":
        return "Still running the tool call."
    if stage == "task":
        return "Still processing the request."
    if fallback:
        return "Still working."
    return "Still processing the request."
