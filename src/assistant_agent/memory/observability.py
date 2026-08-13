"""Minimal trace events for the long-term memory lifecycle."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from assistant_agent.memory.mem0.models import Mem0MemoryChange
from assistant_agent.memory.plugins.contracts import MemoryChange
from assistant_agent.runtime.state import AgentState
from assistant_agent.observability.trace_store import (
    TraceStore,
    append_observability_event,
    new_span_id,
    sanitize_trace_value,
)


_SAFE_PLUGIN_ATTRIBUTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SECRET_PLUGIN_ATTRIBUTE_RE = re.compile(
    r"(?:api[_-]?key|authorization|bearer|credential|password|secret|token)",
    re.IGNORECASE,
)
_KEY_PREFIX_CODE_RE = re.compile(
    r"(?:^|[._-])(?:sk|pk|rk|key)-[A-Za-z0-9_-]{4,}",
    re.IGNORECASE,
)
_BASE64_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{80,}={0,2}$")
_REDACTED_PLUGIN_ATTRIBUTE = "[redacted]"
_INVALID_MEMORY_CODE = "memory_plugin_invalid_result"


@dataclass(frozen=True)
class MemoryObservationContext:
    """Immutable correlation fields safe to retain in background work."""

    trace_id: str
    run_id: str
    user_id: str
    session_id: str

    @classmethod
    def from_state(cls, state: AgentState) -> "MemoryObservationContext":
        return cls(
            trace_id=state.trace_id,
            run_id=state.run_id,
            user_id=state.user_id,
            session_id=state.session_id,
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
    memory_plugin_id: str | None = None,
    memory_plugin_version: str | None = None,
    memory_plugin_api_version: str | None = None,
    memory_plugin_operation: str | None = None,
    memory_plugin_issue_codes: list[str] | None = None,
    memory_plugin_retry_count: int = 0,
) -> None:
    safe_error_codes = _safe_memory_codes(error_codes)
    plugin_attributes = _memory_plugin_attributes(
        plugin_id=memory_plugin_id,
        plugin_version=memory_plugin_version,
        api_version=memory_plugin_api_version,
        operation=memory_plugin_operation,
        issue_codes=memory_plugin_issue_codes or error_codes,
        retry_count=memory_plugin_retry_count,
    )
    _append_memory_event(
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
            "error_codes": safe_error_codes,
            **plugin_attributes,
        },
        output_summary={
            "memory_count": memory_count,
            "error_codes": safe_error_codes,
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
    state: AgentState | None = None,
    context: MemoryObservationContext | None = None,
    pending_count: int,
    memory_plugin_id: str | None = None,
    memory_plugin_version: str | None = None,
    memory_plugin_api_version: str | None = None,
    memory_plugin_operation: str | None = None,
    memory_plugin_issue_codes: list[str] | None = None,
    memory_plugin_retry_count: int = 0,
) -> None:
    resolved = _resolve_context(state=state, context=context)
    _append_memory_event(
        trace_store,
        trace_id=resolved.trace_id,
        run_id=resolved.run_id,
        user_id=resolved.user_id,
        session_id=resolved.session_id,
        canonical_event="memory.ingestion.queued",
        observation_type="event",
        observation_name="memory.ingestion.queued",
        observation_scope="runtime",
        node_name="post_response_memory_ingestion",
        status="queued",
        attributes={
            "pending_count": pending_count,
            **_memory_plugin_attributes(
                plugin_id=memory_plugin_id,
                plugin_version=memory_plugin_version,
                api_version=memory_plugin_api_version,
                operation=memory_plugin_operation,
                issue_codes=memory_plugin_issue_codes,
                retry_count=memory_plugin_retry_count,
            ),
        },
    )


def record_ingestion_finished(
    *,
    trace_store: TraceStore | None,
    state: AgentState | None = None,
    context: MemoryObservationContext | None = None,
    status: str,
    latency_ms: int,
    memory_count: int = 0,
    memory_ids: list[str] | None = None,
    changes: list[Mem0MemoryChange | MemoryChange] | None = None,
    source_turn: str | None = None,
    errors: list[dict[str, Any]] | None = None,
    error: Exception | None = None,
    error_code: str | None = None,
    memory_plugin_id: str | None = None,
    memory_plugin_version: str | None = None,
    memory_plugin_api_version: str | None = None,
    memory_plugin_operation: str | None = None,
    memory_plugin_issue_codes: list[str] | None = None,
    memory_plugin_retry_count: int = 0,
) -> None:
    resolved = _resolve_context(state=state, context=context)
    resolved_changes = list(changes or [])
    resolved_memory_count = (
        len(resolved_changes) if changes is not None else memory_count
    )
    change_counts: dict[str, int] = {}
    for change in resolved_changes:
        operation = _change_operation(change)
        change_counts[operation] = change_counts.get(operation, 0) + 1
    resolved_memory_ids = (
        [change.memory_id for change in resolved_changes]
        if changes is not None
        else list(memory_ids or [])
    )
    summary = {
        "memory_count": resolved_memory_count,
        "change_counts": change_counts,
        "memory_ids": resolved_memory_ids,
        "source_turn": source_turn,
        "errors": _safe_error_summaries(errors),
        **_memory_plugin_attributes(
            plugin_id=memory_plugin_id,
            plugin_version=memory_plugin_version,
            api_version=memory_plugin_api_version,
            operation=memory_plugin_operation,
            issue_codes=memory_plugin_issue_codes,
            retry_count=memory_plugin_retry_count,
        ),
    }
    _append_memory_event(
        trace_store,
        trace_id=resolved.trace_id,
        run_id=resolved.run_id,
        user_id=resolved.user_id,
        session_id=resolved.session_id,
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
        error=(_safe_ingestion_error(error=error, error_code=error_code)),
    )


def _resolve_context(
    *,
    state: AgentState | None,
    context: MemoryObservationContext | None,
) -> MemoryObservationContext:
    if context is not None:
        return context
    if state is None:
        raise TypeError("state or context is required")
    return MemoryObservationContext.from_state(state)


def _append_memory_event(
    trace_store: TraceStore | None,
    **kwargs: Any,
) -> None:
    try:
        append_observability_event(trace_store, **kwargs)
    except Exception:
        return


def _memory_plugin_attributes(
    *,
    plugin_id: str | None,
    plugin_version: str | None,
    api_version: str | None,
    operation: str | None,
    issue_codes: list[str] | None,
    retry_count: int,
) -> dict[str, Any]:
    if plugin_id is None:
        return {}
    return {
        "memory_plugin_id": _safe_plugin_attribute(plugin_id),
        "memory_plugin_version": _safe_plugin_attribute(plugin_version),
        "memory_plugin_api_version": _safe_plugin_attribute(api_version),
        "memory_plugin_operation": _safe_plugin_attribute(operation),
        "memory_plugin_issue_codes": _safe_memory_codes(issue_codes),
        "memory_plugin_retry_count": max(0, retry_count),
    }


def _safe_plugin_attribute(value: str | None) -> str | None:
    if value is None:
        return None
    if not _SAFE_PLUGIN_ATTRIBUTE_RE.fullmatch(
        value
    ) or _SECRET_PLUGIN_ATTRIBUTE_RE.search(value):
        return _REDACTED_PLUGIN_ATTRIBUTE
    return value


def _safe_memory_code(value: object) -> str:
    if (
        type(value) is not str
        or not _SAFE_PLUGIN_ATTRIBUTE_RE.fullmatch(value)
        or _SECRET_PLUGIN_ATTRIBUTE_RE.search(value)
        or _KEY_PREFIX_CODE_RE.search(value)
        or _BASE64_CODE_RE.fullmatch(value)
    ):
        return _INVALID_MEMORY_CODE
    return value


def _safe_memory_codes(values: list[str] | None) -> list[str]:
    return [_safe_memory_code(value) for value in values or []]


def _safe_error_summaries(
    errors: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    safe_errors: list[dict[str, Any]] = []
    for error in errors or []:
        safe_error = dict(error)
        if "code" in safe_error:
            safe_error["code"] = _safe_memory_code(safe_error["code"])
        safe_errors.append(safe_error)
    return safe_errors


def _change_operation(change: Mem0MemoryChange | MemoryChange) -> str:
    if isinstance(change, MemoryChange):
        return change.operation
    return change.event


def _safe_ingestion_error(
    *,
    error: Exception | None,
    error_code: str | None,
) -> dict[str, Any] | None:
    if error_code is not None:
        safe_code = _safe_memory_code(error_code)
        return {"code": safe_code, "message": safe_code}
    if error is None:
        return None
    return {
        "code": "mem0_ingestion_failed",
        "message": sanitize_trace_value(str(error)),
    }
