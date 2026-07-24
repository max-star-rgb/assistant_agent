"""Minimal trace hooks for Mem0 session recall and background capture."""

from __future__ import annotations

from threading import Event
from time import perf_counter
from typing import Any

from assistant_agent.agent.state import AgentState
from assistant_agent.memory.manager import (
    MemoryContext,
    MemoryManager,
    PreparedTurnCapture,
)
from assistant_agent.schemas.identity import RequestIdentity
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.memory_capture_dispatcher import (
    MemoryCaptureDispatcher,
)
from assistant_agent.services.session_memory_context import (
    SessionMemoryContextStore,
)
from assistant_agent.services.trace_store import (
    TraceStore,
    append_observability_event,
    new_span_id,
    sanitize_trace_value,
)


def recall_session_memory_with_trace(
    *,
    manager: MemoryManager,
    trace_store: TraceStore | None,
    trace_id: str | None,
    node_name: str,
    state: AgentState,
    request: UserRequest,
    top_k: int | None = None,
    session_context_store: SessionMemoryContextStore,
    identity: RequestIdentity,
) -> MemoryContext:
    """Recall once at session start and freeze the structured result."""

    started = perf_counter()
    try:
        resolution = session_context_store.resolve(
            identity,
            loader=lambda: manager.recall_session(
                identity,
                top_k=top_k,
            ),
            reset=request.metadata.get("reset_conversation") is True,
        )
        context = resolution.context
    except Exception as exc:
        append_observability_event(
            trace_store,
            trace_id=trace_id or state.trace_id,
            run_id=state.run_id,
            user_id=state.user_id,
            session_id=state.session_id,
            canonical_event="memory.session_recall.finished",
            observation_type="span",
            observation_name="memory.session_recall",
            node_name=node_name,
            status="failed",
            latency_ms=_elapsed_ms(started),
            span_id=new_span_id(),
            error={
                "code": "mem0_recall_failed",
                "message": sanitize_trace_value(str(exc)),
            },
        )
        raise
    if resolution.status == "loaded":
        append_observability_event(
            trace_store,
            trace_id=trace_id or state.trace_id,
            run_id=state.run_id,
            user_id=state.user_id,
            session_id=state.session_id,
            canonical_event="memory.session_recall.finished",
            observation_type="span",
            observation_name="memory.session_recall",
            node_name=node_name,
            status=context.status,
            latency_ms=_elapsed_ms(started),
            span_id=new_span_id(),
            attributes={
                "memory_count": len(context.items),
                "error_codes": context.error_codes,
            },
            output_summary={
                "memory_count": len(context.items),
                "error_codes": context.error_codes,
            },
        )
    return context


def enqueue_memory_capture_with_trace(
    *,
    dispatcher: MemoryCaptureDispatcher,
    manager: MemoryManager,
    trace_store: TraceStore | None,
    trace_id: str | None,
    node_name: str,
    state: AgentState,
) -> bool:
    """Queue Mem0 capture without delaying the foreground response."""

    prepared = manager.prepare_completed_turn_capture(state)
    if prepared is None:
        state.request.metadata["memory_capture"] = {"status": "skipped"}
        return False
    queued = Event()
    submitted = dispatcher.submit(
        ordering_key=prepared.ordering_key,
        callback=lambda: _capture(
            queued=queued,
            manager=manager,
            prepared=prepared,
            trace_store=trace_store,
            trace_id=trace_id or state.trace_id,
            node_name=node_name,
            state=state,
        ),
    )
    if not submitted.accepted:
        state.request.metadata["memory_capture"] = {
            "status": "failed",
            "error_code": submitted.reason,
        }
        return False
    state.request.metadata["memory_capture"] = {
        "status": "queued",
        "pending_count": submitted.pending_count,
    }
    append_observability_event(
        trace_store,
        trace_id=trace_id or state.trace_id,
        run_id=state.run_id,
        user_id=state.user_id,
        session_id=state.session_id,
        canonical_event="memory.capture.queued",
        observation_type="event",
        observation_name="memory.turn_capture",
        observation_scope="runtime",
        node_name=node_name,
        status="queued",
        attributes={"pending_count": submitted.pending_count},
    )
    queued.set()
    return True


def _capture(
    *,
    queued: Event,
    manager: MemoryManager,
    prepared: PreparedTurnCapture,
    trace_store: TraceStore | None,
    trace_id: str,
    node_name: str,
    state: AgentState,
) -> Any:
    queued.wait()
    started = perf_counter()
    span_id = new_span_id()
    try:
        result = manager.capture_prepared_turn(prepared)
    except Exception as exc:
        append_observability_event(
            trace_store,
            trace_id=trace_id,
            run_id=state.run_id,
            user_id=state.user_id,
            session_id=state.session_id,
            canonical_event="memory.capture.finished",
            observation_type="span",
            observation_name="memory.turn_capture",
            observation_scope="runtime",
            node_name=node_name,
            status="failed",
            latency_ms=_elapsed_ms(started),
            span_id=span_id,
            error={
                "code": "mem0_capture_failed",
                "message": sanitize_trace_value(str(exc)),
            },
        )
        return None
    status = (
        "partial"
        if result.errors and result.accepted
        else "failed"
        if not result.accepted
        else "succeeded"
    )
    append_observability_event(
        trace_store,
        trace_id=trace_id,
        run_id=state.run_id,
        user_id=state.user_id,
        session_id=state.session_id,
        canonical_event="memory.capture.finished",
        observation_type="span",
        observation_name="memory.turn_capture",
        observation_scope="runtime",
        node_name=node_name,
        status=status,
        latency_ms=_elapsed_ms(started),
        span_id=span_id,
        attributes={
            "memory_count": len(result.memory_ids),
            "errors": result.errors,
        },
    )
    return result


def _elapsed_ms(started: float) -> int:
    return max(0, int((perf_counter() - started) * 1000))
