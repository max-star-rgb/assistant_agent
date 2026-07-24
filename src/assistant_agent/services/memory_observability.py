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


def load_memory_with_trace(
    *,
    manager: MemoryManager,
    trace_store: TraceStore | None,
    trace_id: str | None,
    node_name: str,
    state: AgentState,
    request: UserRequest,
    top_k: int | None = None,
    session_context_store: SessionMemoryContextStore | None = None,
    session_start: bool = False,
    identity: RequestIdentity | None = None,
) -> MemoryContext:
    """Recall once at session start or reuse the frozen snapshot."""

    started = perf_counter()
    try:
        if session_context_store is None:
            context = (
                manager.load_context_for_request(
                    request,
                    top_k=top_k,
                    session_initial=True,
                    identity=identity,
                )
                if session_start
                else manager.missing_session_snapshot_context()
            )
            snapshot_status = "loaded" if session_start else "missing"
        else:
            resolution = session_context_store.resolve(
                identity
                or RequestIdentity.for_user(
                    user_id=state.user_id,
                    agent_id=state.agent_id,
                    session_id=state.session_id,
                ),
                loader=lambda: manager.load_context_for_request(
                    request,
                    top_k=top_k,
                    session_initial=True,
                    identity=identity,
                ),
                allow_load=session_start,
                reset=(
                    session_start
                    and request.metadata.get("reset_conversation") is True
                ),
            )
            snapshot_status = resolution.status
            context = (
                resolution.context
                if resolution.context is not None
                else manager.missing_session_snapshot_context()
            )
        manager.attach_context_to_state(state, context)
        request.metadata["memory_context_source"] = (
            "mem0_session_start"
            if snapshot_status == "loaded"
            else "session_snapshot"
        )
        request.metadata["memory_session_snapshot_status"] = snapshot_status
    except Exception as exc:
        append_observability_event(
            trace_store,
            trace_id=trace_id or state.trace_id,
            run_id=state.run_id,
            user_id=state.user_id,
            session_id=state.session_id,
            canonical_event=(
                "memory.session_recall.finished"
                if session_start
                else "memory.session_snapshot.missing"
            ),
            observation_type="span",
            observation_name=(
                "memory.session_recall"
                if session_start
                else "memory.session_snapshot"
            ),
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
    append_observability_event(
        trace_store,
        trace_id=trace_id or state.trace_id,
        run_id=state.run_id,
        user_id=state.user_id,
        session_id=state.session_id,
        canonical_event=(
            "memory.session_recall.finished"
            if snapshot_status == "loaded"
            else "memory.session_snapshot.reused"
            if snapshot_status == "reused"
            else "memory.session_snapshot.missing"
        ),
        observation_type="span",
        observation_name=(
            "memory.session_recall"
            if snapshot_status == "loaded"
            else "memory.session_snapshot"
        ),
        node_name=node_name,
        status=context.status,
        latency_ms=_elapsed_ms(started),
        span_id=new_span_id(),
        attributes={
            "session_snapshot_status": snapshot_status,
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
