"""Minimal trace events for the long-term memory lifecycle."""

from __future__ import annotations

from typing import Any

from assistant_agent.agent.state import AgentState
from assistant_agent.services.trace_store import (
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
        observation_name="memory.turn_ingestion",
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
    errors: list[dict[str, Any]] | None = None,
    error: Exception | None = None,
) -> None:
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
        attributes={
            "memory_count": memory_count,
            "errors": list(errors or []),
        },
        error=(
            {
                "code": "mem0_ingestion_failed",
                "message": sanitize_trace_value(str(error)),
            }
            if error is not None
            else None
        ),
    )
