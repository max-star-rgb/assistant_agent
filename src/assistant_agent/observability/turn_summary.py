"""Prompt-safe terminal turn summary trace events."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from assistant_agent.providers.provider_errors import sanitize_error_message
from assistant_agent.observability.trace_store import TraceEvent, TraceStore, new_span_id


ASSISTANT_TURN_SUMMARY_SCHEMA_VERSION = "assistant_turn_summary_v2"
ASSISTANT_TURN_SUMMARY_EVENT = "assistant.turn.summary"
ASSISTANT_TURN_SUMMARY_KEY = "turn_summary"
_FAILURE_MESSAGE_LIMIT = 240
_TERMINAL_EVENTS = {
    "run.completed": "completed",
    "run.failed": "failed",
    "run.cancelled": "cancelled",
}
_CLIENT_TYPES = frozenset(
    {"api", "cli", "gateway", "media_agent", "media_simulator", "unknown"}
)


class AssistantTurnSummary(BaseModel):
    """Stable, prompt-safe terminal fact for one assistant turn."""

    schema_version: Literal["assistant_turn_summary_v2"] = (
        ASSISTANT_TURN_SUMMARY_SCHEMA_VERSION
    )
    trace_id: str
    run_id: str
    turn_id: str | None = None
    user_id: str | None = None
    session_id: str | None = None
    session_turn: int | None = Field(default=None, ge=1)
    client_type: Literal[
        "api",
        "cli",
        "gateway",
        "media_agent",
        "media_simulator",
        "unknown",
    ] = "unknown"
    terminal_status: Literal["completed", "failed", "cancelled", "unknown"] = "unknown"
    entry_status: Literal["completed", "failed", "cancelled", "unknown"] = "unknown"
    runtime_status: Literal[
        "running",
        "pending_cancel",
        "completed",
        "failed",
        "cancelled",
        "unknown",
    ] = "unknown"
    response_present: bool = False
    tool_count: int = Field(default=0, ge=0)
    error_count: int = Field(default=0, ge=0)
    failure_summary: dict[str, Any] | None = None
    latency_summary_ref: dict[str, Any] | None = None


def append_agent_service_turn_summary(
    trace_store: TraceStore | None,
    *,
    timing: Any,
    latency_summary: Any,
    events: list[TraceEvent],
) -> bool:
    """Append the Agent-Service terminal summary after latency correlation exists."""

    trace_id = _optional_string(getattr(timing, "trace_id", None))
    run_id = _optional_string(getattr(timing, "run_id", None))
    if trace_store is None or not trace_id or not run_id:
        return False
    facts = _terminal_facts_from_events(events)
    latency_status = _optional_string(getattr(latency_summary, "status", None))
    terminal_status = facts.get("terminal_status")
    if terminal_status is None:
        terminal_status = "completed" if latency_status == "sent" else "unknown"
    entry_status = "completed" if latency_status == "sent" else "failed"
    runtime_status = _optional_string(getattr(latency_summary, "runtime_status", None)) or str(
        terminal_status
    )
    error_count = _safe_non_negative_int(facts.get("error_count"))
    if error_count is None:
        error_count = 0 if terminal_status == "completed" else 1
    response_present = facts.get("response_present")
    summary = AssistantTurnSummary(
        trace_id=trace_id,
        run_id=run_id,
        turn_id=_optional_string(getattr(timing, "turn_id", None)),
        user_id=_optional_string(getattr(timing, "user_id", None)),
        session_id=_optional_string(getattr(timing, "session_id", None)),
        session_turn=_safe_positive_int(getattr(timing, "session_turn", None)),
        client_type=normalize_client_type(
            _optional_string(getattr(timing, "client_type", None)),
            default="media_agent",
        ),
        terminal_status=_terminal_status(str(terminal_status)),
        entry_status=_entry_status(entry_status),
        runtime_status=_runtime_status(runtime_status),
        response_present=(
            bool(response_present)
            if isinstance(response_present, bool)
            else latency_status == "sent"
        ),
        tool_count=_safe_non_negative_int(facts.get("tool_count")) or 0,
        error_count=error_count,
        failure_summary=(
            facts.get("failure_summary")
            if isinstance(facts.get("failure_summary"), dict)
            else _latency_failure_summary(latency_summary)
            or _fallback_failure_summary(terminal_status)
        ),
        latency_summary_ref=_latency_ref(latency_summary),
    )
    return append_assistant_turn_summary_trace(
        trace_store,
        summary=summary,
        node_name="agent_service",
    )


def append_assistant_turn_summary_trace(
    trace_store: TraceStore | None,
    *,
    summary: AssistantTurnSummary,
    node_name: str,
) -> bool:
    """Append one `assistant.turn.summary` event without prompt content."""

    if trace_store is None:
        return False
    attributes: dict[str, Any] = {
        "run_id": summary.run_id,
        "turn_id": summary.turn_id,
        "session_turn": summary.session_turn,
        "client_type": summary.client_type,
        "terminal_status": summary.terminal_status,
        "entry_status": summary.entry_status,
        "runtime_status": summary.runtime_status,
        "response_present": summary.response_present,
        "tool_count": summary.tool_count,
        "error_count": summary.error_count,
    }
    try:
        trace_store.append(
            TraceEvent(
                trace_id=summary.trace_id,
                run_id=summary.run_id,
                user_id=summary.user_id,
                session_id=summary.session_id,
                node_name=node_name,
                event_type="observability",
                canonical_event=ASSISTANT_TURN_SUMMARY_EVENT,
                span_id=new_span_id(),
                status=summary.terminal_status,
                attributes={
                    key: value for key, value in attributes.items() if value is not None
                },
                output_summary={
                    ASSISTANT_TURN_SUMMARY_KEY: summary.model_dump(mode="json")
                },
            )
        )
    except Exception:
        return False
    return True


def latest_turn_summary_from_events(events: list[TraceEvent]) -> dict[str, Any] | None:
    """Return the latest validated turn summary from trace events."""

    for event in reversed(events):
        if not isinstance(event.output_summary, dict):
            continue
        summary = event.output_summary.get(ASSISTANT_TURN_SUMMARY_KEY)
        if (
            isinstance(summary, dict)
            and summary.get("schema_version")
            in {ASSISTANT_TURN_SUMMARY_SCHEMA_VERSION, "assistant_turn_summary_v1"}
        ):
            try:
                normalized = dict(summary)
                if normalized.get("schema_version") == "assistant_turn_summary_v1":
                    normalized["schema_version"] = ASSISTANT_TURN_SUMMARY_SCHEMA_VERSION
                    normalized["run_id"] = (
                        normalized.pop("assistant_run_id", None)
                        or normalized.pop("gateway_run_id", None)
                    )
                return AssistantTurnSummary.model_validate(normalized).model_dump(mode="json")
            except Exception:
                continue
    return None


def normalize_client_type(value: Any, *, default: str = "unknown") -> Any:
    """Normalize client type to the small public enum."""

    token = str(value or default).strip().lower().replace("-", "_").replace(".", "_")
    if token == "scripts/media_simulator_py":
        token = "media_simulator"
    return token if token in _CLIENT_TYPES else default


def _terminal_status(status: str) -> Any:
    if status == "failed":
        return "failed"
    if status == "cancelled":
        return "cancelled"
    if status == "completed":
        return "completed"
    return "unknown"


def _entry_status(status: str) -> Any:
    return status if status in {"completed", "failed", "cancelled"} else "unknown"


def _runtime_status(status: str) -> Any:
    return (
        status
        if status
        in {"running", "pending_cancel", "completed", "failed", "cancelled", "unknown"}
        else "unknown"
    )


def _safe_positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _safe_non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _optional_string(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _safe_failure_message(value: Any) -> str:
    message = sanitize_error_message(value)
    if len(message) > _FAILURE_MESSAGE_LIMIT:
        return message[: _FAILURE_MESSAGE_LIMIT - 3] + "..."
    return message


def _compact_failure_summary(
    *,
    code: Any = None,
    message: Any = None,
    source: Any = None,
    recovery_action: Any = None,
) -> dict[str, Any] | None:
    payload = {
        "code": _optional_string(sanitize_error_message(code)) if code is not None else None,
        "message": _safe_failure_message(message) if message is not None else None,
        "source": _optional_string(sanitize_error_message(source)) if source is not None else None,
        "recovery_action": _optional_string(sanitize_error_message(recovery_action))
        if recovery_action is not None
        else None,
    }
    result = {key: value for key, value in payload.items() if value not in (None, "")}
    return result or None


def _terminal_facts_from_events(events: list[TraceEvent]) -> dict[str, Any]:
    terminal_event: TraceEvent | None = None
    for event in reversed(events):
        if event.canonical_event in _TERMINAL_EVENTS:
            terminal_event = event
            break
    if terminal_event is None:
        return {}
    attributes = terminal_event.attributes if isinstance(terminal_event.attributes, dict) else {}
    error = terminal_event.error if isinstance(terminal_event.error, dict) else {}
    return {
        "terminal_status": _TERMINAL_EVENTS.get(str(terminal_event.canonical_event)),
        "response_present": attributes.get("response_present"),
        "tool_count": attributes.get("tool_count"),
        "error_count": attributes.get("error_count"),
        "failure_summary": _compact_failure_summary(
            code=error.get("code") or terminal_event.error_code,
            message=error.get("message"),
            source=error.get("source"),
            recovery_action=error.get("recovery_action"),
        ),
    }


def _fallback_failure_summary(terminal_status: Any) -> dict[str, Any] | None:
    if terminal_status not in {"failed", "cancelled"}:
        return None
    code = "agent_run_cancelled" if terminal_status == "cancelled" else "agent_run_failed"
    return {"code": code}


def _latency_failure_summary(latency_summary: Any) -> dict[str, Any] | None:
    code = _optional_string(getattr(latency_summary, "failure_code", None))
    if not code:
        return None
    summary = {"code": code}
    source = _optional_string(getattr(latency_summary, "failure_source", None))
    if source:
        summary["source"] = source
    return summary


def _latency_ref(latency_summary: Any) -> dict[str, Any] | None:
    delivery_id = _optional_string(getattr(latency_summary, "delivery_id", None))
    if not delivery_id:
        return None
    return {
        "canonical_event": "agent_service.turn.finished",
        "delivery_id": delivery_id,
        "trace_id": _optional_string(getattr(latency_summary, "trace_id", None)),
        "run_id": _optional_string(getattr(latency_summary, "run_id", None)),
    }
