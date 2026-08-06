from __future__ import annotations

from datetime import datetime, timezone

from assistant_agent.observability.otel_exporter import TextOtelTraceObserver
from assistant_agent.observability.trace_store import TraceEvent
from assistant_agent.observability.turn_summary import ASSISTANT_TURN_SUMMARY_EVENT


class _RecordingExporter:
    def __init__(self) -> None:
        self.batches = []

    def export(self, spans) -> None:
        self.batches.append(list(spans))


def _event(name: str, *, status: str) -> TraceEvent:
    return TraceEvent(
        trace_id="trace-1",
        run_id="run-1",
        user_id="user-1",
        session_id="session-1",
        node_name="visual_reminder_runtime",
        event_type="observability",
        canonical_event=name,
        observation_type="event",
        observation_name=name,
        status=status,
        attributes={"reminder_id": "reminder-1", "reminder_status": status},
        created_at=datetime(2026, 8, 5, 8, 0, 1, tzinfo=timezone.utc),
    )


def test_visual_reminder_lifecycle_exports_after_turn_summary() -> None:
    exporter = _RecordingExporter()
    observer = TextOtelTraceObserver(exporter, enabled=True)
    summary = TraceEvent(
        trace_id="trace-1",
        run_id="run-1",
        user_id="user-1",
        session_id="session-1",
        node_name="runtime",
        event_type="observability",
        canonical_event=ASSISTANT_TURN_SUMMARY_EVENT,
        status="completed",
        created_at=datetime(2026, 8, 5, 8, 0, tzinfo=timezone.utc),
    )

    observer.on_trace_event(summary)
    observer.on_trace_event(_event("visual_reminder.matched", status="matched"))
    observer.on_trace_event(
        _event("visual_reminder.delivery.finished", status="succeeded")
    )

    assert len(exporter.batches) == 3
    assert [batch[0].name for batch in exporter.batches[1:]] == [
        "visual_reminder.matched",
        "visual_reminder.delivery.finished",
    ]
    assert all(len(batch) == 1 for batch in exporter.batches[1:])
    assert all(batch[0].trace_id == "trace-1" for batch in exporter.batches[1:])
