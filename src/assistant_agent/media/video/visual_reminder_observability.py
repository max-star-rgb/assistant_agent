"""Prompt-safe observability for connection-scoped visual reminders."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from assistant_agent.observability.trace_store import (
    TraceStore,
    append_observability_event,
)


@dataclass(frozen=True)
class VisualReminderTraceContext:
    trace_id: str
    run_id: str
    user_id: str
    session_id: str


def record_visual_reminder_lifecycle(
    trace_store: TraceStore | None,
    *,
    context: VisualReminderTraceContext | None,
    canonical_event: str,
    status: str,
    reminder_id: str,
    attributes: dict[str, Any] | None = None,
) -> None:
    """Append one late-capable lifecycle event without reminder content."""

    if context is None:
        return
    append_observability_event(
        trace_store,
        trace_id=context.trace_id,
        run_id=context.run_id,
        user_id=context.user_id,
        session_id=context.session_id,
        canonical_event=canonical_event,
        observation_type="event",
        observation_name=canonical_event,
        node_name="visual_reminder_runtime",
        status=status,
        attributes={
            "reminder_id": reminder_id,
            **dict(attributes or {}),
        },
    )
