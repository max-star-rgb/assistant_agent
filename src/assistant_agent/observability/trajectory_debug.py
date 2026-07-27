"""Redacted trajectory debug and improvement gate helpers."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel, Field

from assistant_agent.providers.provider_errors import sanitize_error_detail, sanitize_error_message
from assistant_agent.observability.trace_store import TraceEvent


TRAJECTORY_REDACTION = {
    "raw_payloads_included": False,
    "provider_raw_responses_included": False,
    "memory_content_included": False,
    "conversation_history_included": False,
    "media_bodies_included": False,
    "production_mutations_included": False,
}

_SAFE_ATTRIBUTE_KEYS = {
    "budget_ratio",
    "cancel_source",
    "context_usage_ratio",
    "decision_type",
    "output_type",
    "error_count",
    "run_id",
    "iteration",
    "parent_memory_forwarded",
    "provider_latency_ms",
    "recovery_action",
    "response_present",
    "retry_count",
    "risk",
    "runtime_call_latency_ms",
    "side_effect",
    "sla_fallback_emitted",
    "terminal_status",
    "tool_call_id",
    "tool_count",
    "user_visible_event_count",
    "wall_latency_ms",
}
_SAFE_SUMMARY_KEYS = {
    "artifact_id",
    "artifact_ref",
    "budget_exceeded",
    "error_code",
    "item_count",
    "output_ref",
    "result_count",
    "retry_count",
    "status",
    "tool_call_id",
}


class TrajectoryTimelineEvent(BaseModel):
    """One prompt-safe timeline event for trajectory debug replay."""

    index: int = Field(ge=0)
    canonical_event: str | None = None
    event_type: str
    node_name: str
    status: str | None = None
    tool_name: str | None = None
    provider: str | None = None
    model: str | None = None
    error_code: str | None = None
    latency_ms: int | None = Field(default=None, ge=0)
    span_id: str | None = None
    parent_span_id: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    input_summary: dict[str, Any] = Field(default_factory=dict)
    output_summary: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] = Field(default_factory=dict)


class TrajectoryReplayCase(BaseModel):
    """Replay-safe diagnostic case derived from redacted trace events."""

    schema_version: str = "trajectory_replay_case_v1"
    replay_mode: str = "debug_replay_eval_only"
    run_id: str | None = None
    trace_id: str | None = None
    status: str | None = None
    event_count: int = Field(default=0, ge=0)
    timeline: list[TrajectoryTimelineEvent] = Field(default_factory=list)
    raw_data_included: bool = False
    production_mutation_allowed: bool = False
    redaction: dict[str, Any] = Field(default_factory=lambda: dict(TRAJECTORY_REDACTION))


TrajectoryImprovementTarget = Literal["memory", "skill"]


class TrajectoryImprovementGateReport(BaseModel):
    """Manual-review gate for trajectory-derived improvement suggestions."""

    schema_version: str = "trajectory_improvement_gate_v1"
    target: TrajectoryImprovementTarget
    learning_loop_mode: str = "debug_replay_eval_only"
    manual_review_allowed: bool
    production_mutation_allowed: bool = False
    auto_apply_allowed: bool = False
    required_regression_suites: list[str] = Field(default_factory=list)
    blocked_reasons: list[str] = Field(default_factory=list)
    redaction: dict[str, Any] = Field(default_factory=lambda: dict(TRAJECTORY_REDACTION))


def build_redacted_trajectory_replay(events: Iterable[TraceEvent]) -> TrajectoryReplayCase:
    """Build a replay-safe debug case from trace events."""

    source_events = list(events)
    timeline = [_timeline_event(index, event) for index, event in enumerate(source_events)]
    first = source_events[0] if source_events else None
    last = timeline[-1] if timeline else None
    return TrajectoryReplayCase(
        run_id=sanitize_error_message(first.run_id) if first is not None else None,
        trace_id=sanitize_error_message(first.trace_id) if first is not None else None,
        status=last.status if last is not None else None,
        event_count=len(timeline),
        timeline=timeline,
        raw_data_included=False,
        production_mutation_allowed=False,
        redaction=dict(TRAJECTORY_REDACTION),
    )


def evaluate_trajectory_improvement_gate(
    replay: TrajectoryReplayCase,
    *,
    target: TrajectoryImprovementTarget,
    memory_regression_passed: bool = False,
    skill_regression_passed: bool = False,
) -> TrajectoryImprovementGateReport:
    """Evaluate whether a trajectory-derived suggestion may enter manual review."""

    required = ["memory"] if target == "memory" else ["skill"]
    blocked: list[str] = []
    if replay.raw_data_included:
        blocked.append("redacted_replay_required")
    if replay.production_mutation_allowed:
        blocked.append("production_mutation_forbidden")
    if target == "memory" and not memory_regression_passed:
        blocked.append("memory_regression_required")
    if target == "skill" and not skill_regression_passed:
        blocked.append("skill_regression_required")
    return TrajectoryImprovementGateReport(
        target=target,
        manual_review_allowed=not blocked,
        required_regression_suites=required,
        blocked_reasons=blocked,
        production_mutation_allowed=False,
        auto_apply_allowed=False,
        redaction=dict(TRAJECTORY_REDACTION),
    )


def _timeline_event(index: int, event: TraceEvent) -> TrajectoryTimelineEvent:
    error_payload = _safe_error(event)
    error_code = event.error_code or error_payload.get("code")
    return TrajectoryTimelineEvent(
        index=index,
        canonical_event=sanitize_error_message(event.canonical_event) if event.canonical_event else None,
        event_type=event.event_type,
        node_name=sanitize_error_message(event.node_name),
        status=sanitize_error_message(event.status) if event.status else None,
        tool_name=sanitize_error_message(event.tool_name) if event.tool_name else None,
        provider=sanitize_error_message(event.provider) if event.provider else None,
        model=sanitize_error_message(event.model) if event.model else None,
        error_code=sanitize_error_message(error_code) if error_code else None,
        latency_ms=event.latency_ms,
        span_id=sanitize_error_message(event.span_id) if event.span_id else None,
        parent_span_id=sanitize_error_message(event.parent_span_id) if event.parent_span_id else None,
        attributes=_safe_mapping(event.attributes, _SAFE_ATTRIBUTE_KEYS),
        input_summary=_safe_mapping(event.input_summary, _SAFE_SUMMARY_KEYS),
        output_summary=_safe_mapping(event.output_summary, _SAFE_SUMMARY_KEYS),
        error=error_payload,
    )


def _safe_mapping(value: dict[str, Any], allowed_keys: set[str]) -> dict[str, Any]:
    return sanitize_error_detail({key: child for key, child in value.items() if key in allowed_keys})


def _safe_error(event: TraceEvent) -> dict[str, Any]:
    if not isinstance(event.error, dict) or not event.error:
        return {}
    payload: dict[str, Any] = {}
    code = event.error.get("code") or event.error_code
    if code:
        payload["code"] = sanitize_error_message(code)
    if "recoverable" in event.error:
        payload["recoverable"] = bool(event.error["recoverable"])
    if event.error.get("message"):
        payload["message"] = sanitize_error_message(event.error["message"])
    return payload
