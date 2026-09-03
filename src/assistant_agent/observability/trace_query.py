"""Read-only trace and run summary queries."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from assistant_agent.observability.trace_store import TraceEvent, TraceStore, trace_debug_summary
from assistant_agent.observability.turn_summary import latest_turn_summary_from_events


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
    turn_latency: dict[str, Any] | None = None
    turn_summary: dict[str, Any] | None = None


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
    turn_latency: dict[str, Any] | None = None
    turn_summary: dict[str, Any] | None = None
    events: list[dict[str, Any]] = Field(default_factory=list)


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
            budget_exceeded=bool(summary.get("budget_exceeded", False)),
            retry_count=int(summary.get("retry_count", 0)),
            event_count=len(events),
            context=_latest_context_summary(events),
            turn_latency=_latest_turn_latency(events),
            turn_summary=latest_turn_summary_from_events(events),
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
            budget_exceeded=bool(summary.get("budget_exceeded", False)),
            retry_count=int(summary.get("retry_count", 0)),
            context=_latest_context_summary(events),
            turn_latency=_latest_turn_latency(events),
            turn_summary=latest_turn_summary_from_events(events),
            events=summary["events"],
        )


def _latest_context_summary(events: list[TraceEvent]) -> dict[str, Any]:
    for event in reversed(events):
        context = event.output_summary.get("context") if isinstance(event.output_summary, dict) else None
        if isinstance(context, dict):
            return dict(context)
    return {}


def _latest_turn_latency(events: list[TraceEvent]) -> dict[str, Any] | None:
    for event in reversed(events):
        if not isinstance(event.output_summary, dict):
            continue
        summary = event.output_summary.get("turn_latency")
        if (
            isinstance(summary, dict)
            and summary.get("schema_version")
            in {"agent_service_turn_latency_v1", "agent_service_turn_latency_v2"}
        ):
            return dict(summary)
    return None
