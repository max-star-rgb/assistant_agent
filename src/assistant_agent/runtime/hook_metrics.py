"""Metrics observers for observer-only harness hooks."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from assistant_agent.observability.trace_metrics import build_trace_metrics
from assistant_agent.observability.trace_store import TraceEvent, redact_trace_event


class TraceMetricsObserver:
    """In-memory metrics observer derived from redacted trace events."""

    def __init__(self, events: Iterable[TraceEvent] = ()) -> None:
        self._events = [redact_trace_event(event) for event in events]

    @property
    def events(self) -> list[TraceEvent]:
        return list(self._events)

    def on_trace_event(self, event: TraceEvent) -> None:
        self._events.append(redact_trace_event(event))

    def summary(self) -> dict[str, Any]:
        return build_trace_metrics(self._events)

    def clear(self) -> None:
        self._events.clear()
