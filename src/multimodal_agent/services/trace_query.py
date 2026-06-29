"""Read-only trace and run summary queries."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from multimodal_agent.services.trace_store import TraceEvent, TraceStore, trace_debug_summary, trace_event_summary


class RunSummary(BaseModel):
    """Public run summary derived from redacted trace events."""

    run_id: str
    trace_id: str | None = None
    user_id: str | None = None
    session_id: str | None = None
    node_path: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    providers: list[str] = Field(default_factory=list)
    error_count: int = 0
    budget_exceeded: bool = False
    retry_count: int = 0
    event_count: int = 0
    context: dict[str, Any] = Field(default_factory=dict)


class TraceSummary(BaseModel):
    """Public trace summary derived from redacted trace events."""

    trace_id: str
    run_id: str | None = None
    node_path: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    providers: list[str] = Field(default_factory=list)
    error_count: int = 0
    budget_exceeded: bool = False
    retry_count: int = 0
    context: dict[str, Any] = Field(default_factory=dict)
    events: list[dict[str, Any]] = Field(default_factory=list)


class ToolCallSummary(BaseModel):
    """Public tool-call trace summary."""

    run_id: str
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)


class TraceQueryService:
    """Query trace summaries from a TraceStore."""

    def __init__(self, trace_store: TraceStore) -> None:
        self.trace_store = trace_store

    def run_summary(self, run_id: str) -> RunSummary | None:
        events = self.trace_store.list_by_run(run_id)
        if not events:
            return None
        summary = trace_debug_summary(events)
        return RunSummary(
            run_id=run_id,
            trace_id=summary["trace_id"],
            user_id=summary.get("user_id"),
            session_id=summary.get("session_id"),
            node_path=summary["node_path"],
            tools=summary["tools"],
            providers=summary["providers"],
            error_count=summary["error_count"],
            budget_exceeded=summary["budget_exceeded"],
            retry_count=summary["retry_count"],
            event_count=len(events),
            context=_latest_context_summary(events),
        )

    def trace_summary(self, trace_id: str) -> TraceSummary | None:
        events = self.trace_store.list_by_trace(trace_id)
        if not events:
            return None
        summary = trace_debug_summary(events)
        return TraceSummary(
            trace_id=trace_id,
            run_id=summary["run_id"],
            node_path=summary["node_path"],
            tools=summary["tools"],
            providers=summary["providers"],
            error_count=summary["error_count"],
            budget_exceeded=summary["budget_exceeded"],
            retry_count=summary["retry_count"],
            context=_latest_context_summary(events),
            events=summary["events"],
        )

    def tool_calls_by_run(self, run_id: str) -> ToolCallSummary | None:
        events = self.trace_store.list_by_run(run_id)
        if not events:
            return None
        tool_events = [event for event in events if event.tool_name is not None]
        return ToolCallSummary(
            run_id=run_id,
            tool_calls=[_tool_call_summary(event) for event in tool_events],
        )


def _tool_call_summary(event: TraceEvent) -> dict[str, Any]:
    summary = trace_event_summary(event)
    return {
        "trace_id": summary["trace_id"],
        "node_name": summary["node_name"],
        "event_type": summary["event_type"],
        "capability": summary["capability"],
        "tool_name": summary["tool_name"],
        "provider": summary["provider"],
        "model": summary["model"],
        "status": summary["status"],
        "latency_ms": summary["latency_ms"],
        "error_code": summary["error_code"],
        "input_summary": summary["input_summary"],
        "output_summary": summary["output_summary"],
    }


def _latest_context_summary(events: list[TraceEvent]) -> dict[str, Any]:
    latest_context: dict[str, Any] = {}
    for event in reversed(events):
        context = event.output_summary.get("context") if isinstance(event.output_summary, dict) else None
        if isinstance(context, dict):
            latest_context = dict(context)
            break
    memory_promotion = _latest_memory_promotion_summary(events)
    if memory_promotion:
        latest_context.update(memory_promotion)
    return latest_context


def _latest_memory_promotion_summary(events: list[TraceEvent]) -> dict[str, Any]:
    for event in reversed(events):
        summary = event.after_state_summary.get("memory_promotion") if isinstance(event.after_state_summary, dict) else None
        if isinstance(summary, dict):
            return summary
    return {}
