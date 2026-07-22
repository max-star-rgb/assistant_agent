"""Trace helpers for memory lifecycle boundaries."""

from __future__ import annotations

from time import perf_counter
from typing import Any

from assistant_agent.agent.state import AgentState
from assistant_agent.memory.manager import MemoryContext, MemoryManager
from assistant_agent.schemas.memory import MemoryItem
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.trace_store import TraceStore, append_observability_event, new_span_id, sanitize_trace_value


def load_memory_with_trace(
    *,
    manager: MemoryManager,
    trace_store: TraceStore | None,
    trace_id: str | None,
    node_name: str,
    state: AgentState,
    request: UserRequest,
    capability: str | None = None,
    top_k: int | None = None,
    max_context_chars: int | None = None,
    max_context_tokens: int | None = None,
) -> MemoryContext:
    """Load memory context and emit prompt-safe lifecycle trace events."""

    span_id = new_span_id()
    started_at = perf_counter()
    append_observability_event(
        trace_store,
        trace_id=trace_id or state.trace_id,
        run_id=state.run_id,
        user_id=state.user_id,
        session_id=state.session_id,
        canonical_event="memory.load.started",
        node_name=node_name,
        status="started",
        span_id=span_id,
        attributes={
            "capability": capability,
            "query_present": bool((request.text or "").strip()),
            "request_metadata_keys": sorted(request.metadata.keys()),
        },
    )
    try:
        context = manager.load_into_state(
            state,
            request,
            capability=capability,
            top_k=top_k,
            max_context_chars=max_context_chars,
            max_context_tokens=max_context_tokens,
        )
    except Exception as exc:
        append_observability_event(
            trace_store,
            trace_id=trace_id or state.trace_id,
            run_id=state.run_id,
            user_id=state.user_id,
            session_id=state.session_id,
            canonical_event="memory.load.finished",
            node_name=node_name,
            status="failed",
            latency_ms=_elapsed_ms(started_at),
            span_id=span_id,
            error={"code": "memory_load_failed", "message": sanitize_trace_value(str(exc))},
        )
        raise

    summary = memory_load_trace_summary(context)
    append_observability_event(
        trace_store,
        trace_id=trace_id or state.trace_id,
        run_id=state.run_id,
        user_id=state.user_id,
        session_id=state.session_id,
        canonical_event="memory.load.finished",
        node_name=node_name,
        status="succeeded" if context.read_policy_allowed else "skipped",
        latency_ms=_elapsed_ms(started_at),
        span_id=span_id,
        attributes={
            "retrieval_count": 1,
            "retrieved_item_count": summary["retrieved_item_count"],
            "injected_count": summary["injected_count"],
            "memory_context_tokens": summary["memory_context_tokens"],
            "memory_context_budget_tokens": summary["memory_context_budget_tokens"],
            "omitted_count": summary["omitted_count"],
            "rejected_count": summary["rejected_count"],
            "retrieval_version": summary["retrieval_version"],
            "read_policy": summary["read_policy"],
        },
        output_summary={"memory": summary},
    )
    return context


def save_memory_with_trace(
    *,
    manager: MemoryManager,
    trace_store: TraceStore | None,
    trace_id: str | None,
    node_name: str,
    state: AgentState,
    skipped_reason: str | None = None,
) -> MemoryItem | None:
    """Save post-run memory candidate and emit prompt-safe lifecycle trace events."""

    span_id = new_span_id()
    started_at = perf_counter()
    append_observability_event(
        trace_store,
        trace_id=trace_id or state.trace_id,
        run_id=state.run_id,
        user_id=state.user_id,
        session_id=state.session_id,
        canonical_event="memory.save.started",
        node_name=node_name,
        status="started",
        span_id=span_id,
        attributes={
            "state_status": state.status,
            "response_present": state.response is not None,
            "skipped": skipped_reason is not None,
            "skip_reason": skipped_reason,
        },
    )
    try:
        saved = None if skipped_reason else manager.save_from_run(state)
    except Exception as exc:
        append_observability_event(
            trace_store,
            trace_id=trace_id or state.trace_id,
            run_id=state.run_id,
            user_id=state.user_id,
            session_id=state.session_id,
            canonical_event="memory.save.finished",
            node_name=node_name,
            status="failed",
            latency_ms=_elapsed_ms(started_at),
            span_id=span_id,
            error={"code": "memory_save_failed", "message": sanitize_trace_value(str(exc))},
        )
        raise

    summary = memory_save_trace_summary(state, saved=saved, skipped_reason=skipped_reason)
    append_observability_event(
        trace_store,
        trace_id=trace_id or state.trace_id,
        run_id=state.run_id,
        user_id=state.user_id,
        session_id=state.session_id,
        canonical_event="memory.save.finished",
        node_name=node_name,
        status="skipped" if skipped_reason else "succeeded",
        latency_ms=_elapsed_ms(started_at),
        span_id=span_id,
        attributes={
            "save_candidate_count": summary["save_candidate_count"],
            "saved_count": summary["saved_count"],
            "rejected_count": summary["rejected_count"],
            "skipped": summary["skipped"],
            "skip_reason": summary["skip_reason"],
            "written_memory_id": summary["written_memory_id"],
        },
        output_summary={"memory": summary},
    )
    return saved


def capture_memory_with_trace(
    *,
    manager: MemoryManager,
    trace_store: TraceStore | None,
    trace_id: str | None,
    node_name: str,
    state: AgentState,
) -> Any | None:
    """Capture a completed turn without making memory failure fail the user run."""

    span_id = new_span_id()
    started_at = perf_counter()
    append_observability_event(
        trace_store,
        trace_id=trace_id or state.trace_id,
        run_id=state.run_id,
        user_id=state.user_id,
        session_id=state.session_id,
        canonical_event="memory.capture.started",
        node_name=node_name,
        status="started",
        span_id=span_id,
        attributes={"state_status": state.status, "response_present": state.response is not None},
    )
    try:
        result = manager.capture_completed_turn(state)
    except Exception as exc:
        append_observability_event(
            trace_store,
            trace_id=trace_id or state.trace_id,
            run_id=state.run_id,
            user_id=state.user_id,
            session_id=state.session_id,
            canonical_event="memory.capture.finished",
            node_name=node_name,
            status="failed",
            latency_ms=_elapsed_ms(started_at),
            span_id=span_id,
            error={"code": "memory_capture_failed", "message": sanitize_trace_value(str(exc))},
        )
        state.request.metadata["memory_capture"] = {
            "status": "failed",
            "error_code": "memory_capture_failed",
        }
        return None
    daily_ids = list(getattr(result, "daily_engine_ids", []) or []) if result is not None else []
    core_ids = list(getattr(result, "core_engine_ids", []) or []) if result is not None else []
    errors = list(getattr(result, "errors", []) or []) if result is not None else []
    accepted = bool(getattr(result, "accepted", False)) if result is not None else False
    if result is None:
        status = "skipped"
    elif errors and accepted:
        status = "partial"
    elif not accepted:
        status = "failed"
    else:
        status = "succeeded"
    state.request.metadata["memory_capture"] = {
        "status": status,
        "daily_count": len(daily_ids),
        "core_count": len(core_ids),
        "error_count": len(errors),
    }
    append_observability_event(
        trace_store,
        trace_id=trace_id or state.trace_id,
        run_id=state.run_id,
        user_id=state.user_id,
        session_id=state.session_id,
        canonical_event="memory.capture.finished",
        node_name=node_name,
        status=status,
        latency_ms=_elapsed_ms(started_at),
        span_id=span_id,
        attributes={
            "daily_count": len(daily_ids),
            "core_count": len(core_ids),
            "error_count": len(errors),
        },
    )
    return result


def memory_load_trace_summary(context: MemoryContext) -> dict[str, Any]:
    """Return a memory-load trace summary without memory text or summaries."""

    return {
        "retrieval_count": 1,
        "retrieved_item_count": len(context.items) + context.omitted_count,
        "injected_count": len(context.items),
        "injected_memory_ids": [item.memory_id for item in context.items],
        "memory_layers": [block.layer for block in context.blocks],
        "memory_context_tokens": context.total_tokens,
        "memory_context_budget_tokens": context.budget_tokens,
        "omitted_count": context.omitted_count,
        "rejected_count": len(context.rejected_reasons),
        "rejected_reasons": context.rejected_reasons[:8],
        "retrieval_version": context.retrieval_version,
        "read_policy": context.read_policy or {
            "allowed": context.read_policy_allowed,
            "reason": context.read_policy_reason,
        },
    }


def memory_save_trace_summary(
    state: AgentState,
    *,
    saved: MemoryItem | None,
    skipped_reason: str | None = None,
) -> dict[str, Any]:
    """Return a memory-save trace summary without candidate or memory content."""

    metadata = state.request.metadata
    auto_summary = _auto_task_summary(metadata.get("auto_task_summary_memory"), skipped_reason=skipped_reason)
    return {
        "save_candidate_count": _metadata_int(metadata, "memory_promotion_candidates"),
        "saved_count": _metadata_int(metadata, "memory_promotion_written"),
        "rejected_count": _metadata_int(metadata, "memory_promotion_rejected"),
        "skipped": bool(auto_summary.get("skipped")),
        "skip_reason": auto_summary.get("reason"),
        "candidate": auto_summary.get("candidate"),
        "written_memory_id": saved.memory_id if saved is not None else auto_summary.get("memory_id"),
        "written_memory_type": saved.memory_type if saved is not None else None,
        "candidate_decisions": _safe_candidate_decisions(metadata.get("memory_promotion_candidate_audit")),
    }


def _elapsed_ms(started_at: float) -> int:
    return max(0, int((perf_counter() - started_at) * 1000))


def _auto_task_summary(value: Any, *, skipped_reason: str | None) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"skipped": skipped_reason is not None, "reason": skipped_reason}
    return {
        "skipped": value.get("skipped") is True or skipped_reason is not None,
        "reason": _safe_text(value.get("reason") or skipped_reason),
        "candidate": value.get("candidate") if isinstance(value.get("candidate"), bool) else None,
        "written": value.get("written") if isinstance(value.get("written"), bool) else None,
        "memory_id": _safe_text(value.get("memory_id")),
    }


def _safe_candidate_decisions(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [_safe_candidate_decision(item) for item in value[-5:] if isinstance(item, dict)]


def _safe_candidate_decision(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": _safe_text(value.get("kind")),
        "memory_type": _safe_text(value.get("memory_type")),
        "allowed": value.get("allowed") if isinstance(value.get("allowed"), bool) else None,
        "destination": _safe_text(value.get("destination")),
        "written": value.get("written") if isinstance(value.get("written"), bool) else None,
        "written_memory_id": _safe_text(value.get("written_memory_id")),
        "reason": _safe_text(value.get("reason")),
        "user_intent_explicit": value.get("user_intent_explicit")
        if isinstance(value.get("user_intent_explicit"), bool)
        else None,
        "require_user_confirmation": value.get("require_user_confirmation")
        if isinstance(value.get("require_user_confirmation"), bool)
        else None,
        "sensitivity": _safe_text(value.get("sensitivity")),
        "ttl_days": _metadata_int(value, "ttl_days"),
    }


def _safe_text(value: Any) -> str | None:
    return sanitize_trace_value(value) if isinstance(value, str) and value else None


def _metadata_int(metadata: dict[str, Any], key: str) -> int:
    value = metadata.get(key)
    return value if isinstance(value, int) and value >= 0 else 0
