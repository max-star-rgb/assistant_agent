"""Prompt-safe text turn diagnostics derived from structured trace facts."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field

from assistant_agent.observability.trace_store import TraceEvent, redact_trace_event, sanitize_trace_value
from assistant_agent.observability.turn_summary import (
    ASSISTANT_TURN_SUMMARY_KEY,
    ASSISTANT_TURN_SUMMARY_SCHEMA_VERSION,
)


TURN_DIAGNOSTIC_SCHEMA_VERSION = "assistant_agent_turn_diagnostic_v1"
TaskOutcome = Literal["success", "degraded", "failed", "unknown"]
ExecutionStatus = Literal["success", "pending_cancel", "failed", "cancelled", "unknown"]
DeliveryStatus = Literal["success", "failed", "response_ready", "unknown"]
UxStatus = Literal["measured", "failed", "unknown"]

_TASK_OUTCOMES = {"success", "degraded", "failed", "unknown"}
_DELIVERY_SUCCESS_STATUSES = {"sent", "acked", "success", "completed"}
_READ_ONLY_TOOL_NAMES = {"web_search", "web_fetch"}


class ToolLatencySummary(BaseModel):
    """Prompt-safe aggregate for one tool in a text turn."""

    tool_name: str
    count: int = Field(ge=0)
    total_latency_ms: int = Field(ge=0)


class LlmLatencySummary(BaseModel):
    """Prompt-safe aggregate for LLM latency diagnostics."""

    count: int = Field(default=0, ge=0)
    wall_latency_ms: int | None = Field(default=None, ge=0)
    provider_latency_ms: int | None = Field(default=None, ge=0)
    overhead_ms: int | None = Field(default=None, ge=0)
    max_wall_latency_ms: int | None = Field(default=None, ge=0)
    max_provider_latency_ms: int | None = Field(default=None, ge=0)
    max_overhead_ms: int | None = Field(default=None, ge=0)


class TurnDiagnostic(BaseModel):
    """Text turn diagnostic summary for console and external observability."""

    schema_version: Literal["assistant_agent_turn_diagnostic_v1"] = TURN_DIAGNOSTIC_SCHEMA_VERSION
    execution_status: ExecutionStatus = "unknown"
    delivery_status: DeliveryStatus = "unknown"
    task_outcome: TaskOutcome = "unknown"
    text_ux_status: UxStatus = "unknown"
    prerequisites: tuple[str, ...] = ()
    unresolved_prerequisites: tuple[str, ...] = ()
    location_source: str | None = None
    clarification_too_late: bool = False
    unnecessary_tool_calls: int = Field(default=0, ge=0)
    total_latency_ms: int | None = Field(default=None, ge=0)
    first_text_latency_ms: int | None = Field(default=None, ge=0)
    llm_summary: LlmLatencySummary = Field(default_factory=LlmLatencySummary)
    tool_summary: tuple[ToolLatencySummary, ...] = ()
    context_peak_ratio: float | None = Field(default=None, ge=0)
    decision_path: tuple[str, ...] = ()
    diagnostic_flags: tuple[str, ...] = ()
    suggested_actions: tuple[str, ...] = ()

    def langfuse_trace_metadata(self) -> dict[str, Any]:
        """Return safe Langfuse trace metadata attributes."""

        metadata: dict[str, Any] = {
            "langfuse.trace.metadata.execution_status": self.execution_status,
            "langfuse.trace.metadata.delivery_status": self.delivery_status,
            "langfuse.trace.metadata.task_outcome": self.task_outcome,
            "langfuse.trace.metadata.text_ux_status": self.text_ux_status,
            "langfuse.trace.metadata.clarification_too_late": self.clarification_too_late,
            "langfuse.trace.metadata.unnecessary_tool_calls": self.unnecessary_tool_calls,
        }
        if self.prerequisites:
            metadata["langfuse.trace.metadata.prerequisites"] = list(self.prerequisites)
        if self.unresolved_prerequisites:
            metadata["langfuse.trace.metadata.unresolved_prerequisites"] = list(self.unresolved_prerequisites)
        if self.location_source:
            metadata["langfuse.trace.metadata.location_source"] = self.location_source
        if self.context_peak_ratio is not None:
            metadata["langfuse.trace.metadata.context_peak_ratio"] = self.context_peak_ratio
        if self.llm_summary.max_overhead_ms is not None:
            metadata["langfuse.trace.metadata.llm_max_overhead_ms"] = self.llm_summary.max_overhead_ms
        if self.first_text_latency_ms is not None:
            metadata["langfuse.trace.metadata.first_text_latency_ms"] = self.first_text_latency_ms
        return metadata


def build_turn_diagnostic(
    events: Iterable[TraceEvent | Mapping[str, Any]],
    *,
    payload: Mapping[str, Any] | None = None,
) -> TurnDiagnostic:
    """Build one prompt-safe text turn diagnostic from structured facts only."""

    event_items = [_event_mapping(event) for event in events]
    payload_mapping = payload or {}
    turn_summary = _payload_turn_summary(payload_mapping) or _latest_turn_summary(event_items)
    turn_latency = _mapping_or_empty(payload_mapping.get("turn_latency")) or _latest_turn_latency(event_items)
    evaluation = _structured_evaluation(event_items, payload_mapping, turn_summary)
    first_text_latency_ms = _first_int(
        turn_latency,
        ("first_text_latency_ms", "first_stream_chunk_latency_ms", "first_token_latency_ms"),
    )
    llm_summary = _llm_latency_summary(event_items)
    tool_summary = tuple(_tool_latency_summary(event_items))
    context_peak = _context_peak_ratio(event_items)
    task_outcome = _task_outcome(evaluation)
    flags = _diagnostic_flags(
        llm_summary=llm_summary,
        context_peak=context_peak,
        turn_latency=turn_latency,
        first_text_latency_ms=first_text_latency_ms,
        tool_summary=tool_summary,
        unresolved_prerequisites=evaluation["unresolved_prerequisites"],
        clarification_too_late=evaluation["clarification_too_late"],
        unnecessary_tool_calls=evaluation["unnecessary_tool_calls"],
    )
    return TurnDiagnostic(
        execution_status=_execution_status(payload_mapping, turn_summary),
        delivery_status=_delivery_status(turn_latency, turn_summary),
        task_outcome=task_outcome,
        text_ux_status=_text_ux_status(
            first_text_latency_ms=first_text_latency_ms,
            turn_summary=turn_summary,
            turn_latency=turn_latency,
        ),
        prerequisites=tuple(evaluation["prerequisites"]),
        unresolved_prerequisites=tuple(evaluation["unresolved_prerequisites"]),
        location_source=evaluation["location_source"],
        clarification_too_late=evaluation["clarification_too_late"],
        unnecessary_tool_calls=evaluation["unnecessary_tool_calls"],
        total_latency_ms=_first_int(turn_latency, ("total_ms",)) or _safe_int(payload_mapping.get("duration_ms")),
        first_text_latency_ms=first_text_latency_ms,
        llm_summary=llm_summary,
        tool_summary=tool_summary,
        context_peak_ratio=context_peak,
        decision_path=tuple(_decision_path(event_items, llm_summary=llm_summary, tool_summary=tool_summary)),
        diagnostic_flags=tuple(flags),
        suggested_actions=tuple(_suggested_actions(flags)),
    )


def _event_mapping(event: TraceEvent | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(event, TraceEvent):
        return redact_trace_event(event).model_dump(mode="python")
    return dict(event)


def _payload_turn_summary(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    value = payload.get("turn_summary")
    return dict(value) if _is_turn_summary(value) else None


def _latest_turn_summary(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    for event in reversed(events):
        output_summary = _mapping_or_empty(event.get("output_summary"))
        value = output_summary.get(ASSISTANT_TURN_SUMMARY_KEY)
        if _is_turn_summary(value):
            return dict(value)
    return {}


def _is_turn_summary(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("schema_version") == ASSISTANT_TURN_SUMMARY_SCHEMA_VERSION
    )


def _latest_turn_latency(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    for event in reversed(events):
        if _event_name(event) == "agent_service.turn.finished":
            attributes = _mapping_or_empty(event.get("attributes"))
            output_summary = _mapping_or_empty(event.get("output_summary"))
            nested = _mapping_or_empty(output_summary.get("turn_latency"))
            return {
                **nested,
                **attributes,
                "status": event.get("status") or nested.get("status"),
            }
    return {}


def _structured_evaluation(
    events: Sequence[Mapping[str, Any]],
    payload: Mapping[str, Any],
    turn_summary: Mapping[str, Any],
) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    for source in (payload, turn_summary):
        _merge_evaluation_source(facts, source)
    for event in events:
        _merge_evaluation_source(facts, _mapping_or_empty(event.get("attributes")))
        _merge_evaluation_source(facts, _mapping_or_empty(event.get("output_summary")))
        nested = _mapping_or_empty(_mapping_or_empty(event.get("output_summary")).get("turn_diagnostic"))
        _merge_evaluation_source(facts, nested)
    prerequisites = _string_tuple(facts.get("prerequisites"))
    unresolved = _string_tuple(facts.get("unresolved_prerequisites"))
    location_source = _safe_string(facts.get("location_source"))
    task_outcome = _safe_task_outcome(facts.get("task_outcome"))
    if task_outcome is None and unresolved:
        task_outcome = "degraded"
    return {
        "task_outcome": task_outcome,
        "prerequisites": prerequisites,
        "unresolved_prerequisites": unresolved,
        "location_source": location_source,
        "clarification_too_late": _safe_bool(facts.get("clarification_too_late")),
        "unnecessary_tool_calls": _safe_non_negative_int(facts.get("unnecessary_tool_calls")) or 0,
    }


def _merge_evaluation_source(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    for key in (
        "task_outcome",
        "prerequisites",
        "unresolved_prerequisites",
        "location_source",
        "clarification_too_late",
        "unnecessary_tool_calls",
    ):
        value = source.get(key)
        if value is not None:
            target[key] = value


def _task_outcome(evaluation: Mapping[str, Any]) -> TaskOutcome:
    value = evaluation.get("task_outcome")
    return value if isinstance(value, str) and value in _TASK_OUTCOMES else "unknown"


def _execution_status(payload: Mapping[str, Any], turn_summary: Mapping[str, Any]) -> ExecutionStatus:
    runtime_status = str(turn_summary.get("runtime_status") or "")
    if runtime_status == "pending_cancel":
        return "pending_cancel"
    status = str(turn_summary.get("terminal_status") or payload.get("status") or "unknown")
    error_count = turn_summary.get("error_count")
    if not isinstance(error_count, int):
        error_count = payload.get("error_count")
    if status == "completed" and error_count == 0:
        return "success"
    if status in {"failed", "cancelled"}:
        return status
    if isinstance(error_count, int) and error_count > 0:
        return "failed"
    return "unknown"


def _text_ux_status(
    *,
    first_text_latency_ms: int | None,
    turn_summary: Mapping[str, Any],
    turn_latency: Mapping[str, Any],
) -> UxStatus:
    if first_text_latency_ms is not None:
        return "measured"
    if turn_summary.get("entry_status") == "failed" or turn_latency.get("status") == "failed":
        return "failed"
    return "unknown"


def _delivery_status(turn_latency: Mapping[str, Any], turn_summary: Mapping[str, Any]) -> DeliveryStatus:
    status = str(turn_latency.get("status") or "").lower()
    ack = str(turn_latency.get("ack_status") or "").lower()
    if status in _DELIVERY_SUCCESS_STATUSES or ack == "acked":
        return "success"
    if status in {"failed", "disconnected_before_send"}:
        return "failed"
    if turn_summary.get("response_present") is True:
        return "response_ready"
    return "unknown"


def _llm_latency_summary(events: Sequence[Mapping[str, Any]]) -> LlmLatencySummary:
    count = 0
    wall_total = 0
    provider_total = 0
    max_wall = 0
    max_provider = 0
    for event in events:
        if _event_name(event) != "llm.chat.finished":
            continue
        count += 1
        attributes = _mapping_or_empty(event.get("attributes"))
        wall = _safe_int(attributes.get("wall_latency_ms"))
        if wall is None:
            wall = _safe_int(event.get("latency_ms")) or 0
        provider = _safe_int(attributes.get("provider_latency_ms"))
        wall_total += wall
        max_wall = max(max_wall, wall)
        if provider is not None:
            provider_total += provider
            max_provider = max(max_provider, provider)
    max_overhead = max(0, max_wall - max_provider) if max_provider else None
    return LlmLatencySummary(
        count=count,
        wall_latency_ms=wall_total if count else None,
        provider_latency_ms=provider_total if provider_total else None,
        overhead_ms=max(0, wall_total - provider_total) if provider_total else None,
        max_wall_latency_ms=max_wall if count else None,
        max_provider_latency_ms=max_provider if max_provider else None,
        max_overhead_ms=max_overhead,
    )


def _tool_latency_summary(events: Sequence[Mapping[str, Any]]) -> list[ToolLatencySummary]:
    summaries: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for event in events:
        if _event_name(event) not in {"tool.finished", "tool.failed"}:
            continue
        tool_name = _safe_string(event.get("tool_name")) or "unknown_tool"
        if tool_name not in summaries:
            summaries[tool_name] = {"tool_name": tool_name, "count": 0, "total_latency_ms": 0}
            order.append(tool_name)
        summary = summaries[tool_name]
        summary["count"] += 1
        latency_ms = _safe_int(event.get("latency_ms"))
        if latency_ms is not None:
            summary["total_latency_ms"] += latency_ms
    return [ToolLatencySummary(**summaries[name]) for name in order]


def _context_peak_ratio(events: Sequence[Mapping[str, Any]]) -> float | None:
    peak: float | None = None
    for event in events:
        candidates: list[Any] = []
        attributes = _mapping_or_empty(event.get("attributes"))
        candidates.append(attributes.get("context_usage_ratio"))
        output_summary = _mapping_or_empty(event.get("output_summary"))
        context = _mapping_or_empty(output_summary.get("context"))
        budget = _mapping_or_empty(context.get("budget"))
        candidates.append(budget.get("context_usage_ratio"))
        for value in candidates:
            ratio = _ratio_value(value)
            if ratio is not None:
                peak = ratio if peak is None else max(peak, ratio)
    return peak


def _decision_path(
    events: Sequence[Mapping[str, Any]],
    *,
    llm_summary: LlmLatencySummary,
    tool_summary: Sequence[ToolLatencySummary],
) -> list[str]:
    path: list[str] = []
    if llm_summary.count:
        path.append(f"LLM chat x{llm_summary.count}")
    outputs = [
        event
        for event in events
        if _event_name(event) in {"assistant.output", "react.decision"}
    ]
    for event in outputs[:3]:
        output_summary = _mapping_or_empty(event.get("output_summary"))
        attributes = _mapping_or_empty(event.get("attributes"))
        output_type = (
            output_summary.get("output_type")
            or attributes.get("output_type")
            or output_summary.get("decision_type")
            or attributes.get("decision_type")
            or event.get("status")
            or "unknown"
        )
        tool = _safe_string(event.get("tool_name"))
        suffix = f" {tool}" if tool else ""
        path.append(f"Assistant output {output_type}{suffix}")
    for tool in tool_summary:
        path.append(f"Tool {tool.tool_name} x{tool.count}")
    return path


def _diagnostic_flags(
    *,
    llm_summary: LlmLatencySummary,
    context_peak: float | None,
    turn_latency: Mapping[str, Any],
    first_text_latency_ms: int | None,
    tool_summary: Sequence[ToolLatencySummary],
    unresolved_prerequisites: Sequence[str],
    clarification_too_late: bool,
    unnecessary_tool_calls: int,
) -> list[str]:
    flags: list[str] = []
    failure_code = _safe_string(turn_latency.get("failure_code"))
    if failure_code:
        active_stage = _safe_string(turn_latency.get("active_stage"))
        suffix = f" while {active_stage} remained active" if active_stage else ""
        flags.append(f"P0 {failure_code}{suffix}")
    if unresolved_prerequisites:
        flags.append(f"P0 unresolved prerequisites: {', '.join(unresolved_prerequisites)}")
    if clarification_too_late:
        flags.append("P0 clarification happened after tool calls")
    overhead = llm_summary.max_overhead_ms
    provider = llm_summary.max_provider_latency_ms
    if isinstance(overhead, int) and overhead >= 1000 and isinstance(provider, int):
        flags.append(f"P0 LLM overhead {overhead}ms exceeds provider latency")
    if isinstance(context_peak, float) and context_peak >= 0.8:
        flags.append(f"P1 Context peak {_percent(context_peak)}")
    delivery_status = str(turn_latency.get("status") or "").lower()
    if delivery_status in _DELIVERY_SUCCESS_STATUSES and first_text_latency_ms is None:
        flags.append("P1 first text latency is missing")
    if unnecessary_tool_calls:
        flags.append(f"P1 unnecessary tool calls {unnecessary_tool_calls}")
    if any(
        tool.tool_name in _READ_ONLY_TOOL_NAMES
        and tool.count >= 3
        and tool.total_latency_ms >= 3000
        for tool in tool_summary
    ):
        flags.append("P1 repeated read-only tool calls may be serial or over-broad")
    return flags


def _suggested_actions(flags: Sequence[str]) -> list[str]:
    suggestions: list[str] = []
    if any("gateway_turn_timeout" in flag for flag in flags):
        suggestions.append("Inspect the active partial-trace stage before changing the entry deadline.")
    if any("unresolved prerequisites" in flag for flag in flags):
        suggestions.append("Resolve required task prerequisites before tools or answer with an explicit limitation.")
    if any("clarification happened" in flag for flag in flags):
        suggestions.append("Ask required clarifying questions before spending tool calls.")
    if any("LLM overhead" in flag for flag in flags):
        suggestions.append("Break down LLM queue, request build, TTFT, stream consume, parse, and finalize timing.")
    if any("Context peak" in flag for flag in flags):
        suggestions.append("Inspect system prompt, tool schemas, and tool observations as primary context contributors.")
    if any("first text latency" in flag for flag in flags):
        suggestions.append("Record first text response latency for text turns.")
    if any("unnecessary tool calls" in flag or "read-only tool calls" in flag for flag in flags):
        suggestions.append("Review whether same-batch read-only tool calls can run concurrently or be narrowed.")
    return suggestions


def _event_name(event: Mapping[str, Any]) -> str:
    return str(event.get("canonical_event") or event.get("event_type") or event.get("node_name") or "event")


def _mapping_or_empty(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _first_int(source: Mapping[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = _safe_int(source.get(key))
        if value is not None:
            return value
    return None


def _safe_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _safe_non_negative_int(value: Any) -> int | None:
    integer = _safe_int(value)
    if integer is None or integer < 0:
        return None
    return integer


def _safe_bool(value: Any) -> bool:
    return value if isinstance(value, bool) else False


def _safe_string(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return sanitize_trace_value(value)
    return None


def _safe_task_outcome(value: Any) -> str | None:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TASK_OUTCOMES:
            return normalized
    return None


def _string_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str) and value:
        return (_safe_string(value) or value,)
    if isinstance(value, Iterable) and not isinstance(value, dict):
        result: list[str] = []
        for item in value:
            text = _safe_string(item)
            if text and text not in result:
                result.append(text)
        return tuple(result)
    return ()


def _ratio_value(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        ratio = float(value)
        if ratio > 1:
            ratio = ratio / 100
        return max(0.0, ratio)
    return None


def _percent(value: float) -> str:
    return f"{value * 100:.1f}%"
