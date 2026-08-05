"""Minimal trace events for the long-term memory lifecycle."""

from __future__ import annotations

from typing import Any

from assistant_agent.memory.mem0.models import Mem0MemoryChange
from assistant_agent.memory.trace_content import (
    MemoryIngestionTraceContent,
    MemoryTraceContentStore,
    get_default_memory_trace_content_store,
)
from assistant_agent.observability.trace_content_policy import (
    local_memory_trace_content_enabled,
)
from assistant_agent.runtime.state import AgentState
from assistant_agent.observability.trace_store import (
    TraceStore,
    append_observability_event,
    new_span_id,
    sanitize_trace_value,
)


def record_session_recall(
    *,
    trace_store: TraceStore | None,
    state: AgentState,
    status: str,
    latency_ms: int,
    memory_count: int = 0,
    error_codes: list[str] | None = None,
    error: Exception | None = None,
) -> None:
    append_observability_event(
        trace_store,
        trace_id=state.trace_id,
        run_id=state.run_id,
        user_id=state.user_id,
        session_id=state.session_id,
        canonical_event="memory.session_recall.finished",
        observation_type="span",
        observation_name="memory.session_recall",
        node_name="session_start",
        status=status,
        latency_ms=latency_ms,
        span_id=new_span_id(),
        attributes={
            "memory_count": memory_count,
            "error_codes": list(error_codes or []),
        },
        output_summary={
            "memory_count": memory_count,
            "error_codes": list(error_codes or []),
        },
        error=(
            {
                "code": "mem0_recall_failed",
                "message": sanitize_trace_value(str(error)),
            }
            if error is not None
            else None
        ),
    )


def record_ingestion_queued(
    *,
    trace_store: TraceStore | None,
    state: AgentState,
    pending_count: int,
) -> None:
    append_observability_event(
        trace_store,
        trace_id=state.trace_id,
        run_id=state.run_id,
        user_id=state.user_id,
        session_id=state.session_id,
        canonical_event="memory.ingestion.queued",
        observation_type="event",
        observation_name="memory.ingestion.queued",
        observation_scope="runtime",
        node_name="post_response_memory_ingestion",
        status="queued",
        attributes={"pending_count": pending_count},
    )


def record_ingestion_finished(
    *,
    trace_store: TraceStore | None,
    state: AgentState,
    status: str,
    latency_ms: int,
    memory_count: int = 0,
    memory_ids: list[str] | None = None,
    changes: list[Mem0MemoryChange] | None = None,
    source_turn: str | None = None,
    source_user_text: str | None = None,
    source_assistant_text: str | None = None,
    content_store: MemoryTraceContentStore | None = None,
    errors: list[dict[str, Any]] | None = None,
    error: Exception | None = None,
) -> None:
    resolved_changes = list(changes or [])
    resolved_memory_count = len(resolved_changes) if changes is not None else memory_count
    change_counts: dict[str, int] = {}
    for change in resolved_changes:
        change_counts[change.event] = change_counts.get(change.event, 0) + 1
    resolved_memory_ids = (
        [change.memory_id for change in resolved_changes]
        if changes is not None
        else list(memory_ids or [])
    )
    content_capture_status = "disabled"
    content_capture_enabled = local_memory_trace_content_enabled()
    if content_capture_enabled:
        content_capture_status = "skipped"
    if (
        content_capture_enabled
        and source_turn
        and state.user_id
        and state.session_id
    ):
        try:
            (content_store or get_default_memory_trace_content_store()).put(
                MemoryIngestionTraceContent(
                    trace_id=state.trace_id,
                    run_id=state.run_id,
                    user_id=state.user_id,
                    session_id=state.session_id,
                    source_turn=source_turn,
                    user_text=source_user_text,
                    assistant_text=source_assistant_text,
                    changes=resolved_changes,
                )
            )
            content_capture_status = "captured"
        except Exception:
            content_capture_status = "failed"
    summary = {
        "memory_count": resolved_memory_count,
        "change_counts": change_counts,
        "memory_ids": resolved_memory_ids,
        "source_turn": source_turn,
        "errors": list(errors or []),
        "content_capture_status": content_capture_status,
    }
    append_observability_event(
        trace_store,
        trace_id=state.trace_id,
        run_id=state.run_id,
        user_id=state.user_id,
        session_id=state.session_id,
        canonical_event="memory.ingestion.finished",
        observation_type="span",
        observation_name="memory.turn_ingestion",
        observation_scope="runtime",
        node_name="post_response_memory_ingestion",
        status=status,
        latency_ms=latency_ms,
        span_id=new_span_id(),
        attributes=summary,
        output_summary=summary,
        error=(
            {
                "code": "mem0_ingestion_failed",
                "message": sanitize_trace_value(str(error)),
            }
            if error is not None
            else None
        ),
    )
