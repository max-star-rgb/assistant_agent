"""Read-only trace and run summary queries."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from assistant_agent.context.models import ContextReport
from assistant_agent.context.report import (
    context_report_from_trace_context_summary,
    context_report_v2_from_v1,
)
from assistant_agent.observability.trace_store import TraceEvent, TraceStore, trace_debug_summary, trace_event_summary
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


class ToolCallSummary(BaseModel):
    """Public tool-call trace summary."""

    run_id: str
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)


class ContextReportQueryResult(BaseModel):
    """Public context report lookup result."""

    run_id: str | None = None
    trace_id: str | None = None
    context_report_v2: ContextReport


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
            budget_exceeded=summary["budget_exceeded"],
            retry_count=summary["retry_count"],
            context=_latest_context_summary(events),
            turn_latency=_latest_turn_latency(events),
            turn_summary=latest_turn_summary_from_events(events),
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

    def context_by_run(self, run_id: str) -> ContextReportQueryResult | None:
        events = self.trace_store.list_by_run(run_id)
        if not events:
            return None
        return ContextReportQueryResult(
            run_id=run_id,
            trace_id=events[0].trace_id,
            context_report_v2=_latest_context_report(events),
        )

    def context_by_trace(self, trace_id: str) -> ContextReportQueryResult | None:
        events = self.trace_store.list_by_trace(trace_id)
        if not events:
            return None
        return ContextReportQueryResult(
            run_id=events[0].run_id,
            trace_id=trace_id,
            context_report_v2=_latest_context_report(events),
        )


def _tool_call_summary(event: TraceEvent) -> dict[str, Any]:
    summary = trace_event_summary(event)
    attributes = summary["attributes"]
    return {
        "trace_id": summary["trace_id"],
        "tool_call_id": attributes.get("tool_call_id"),
        "step_id": attributes.get("step_id"),
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


def _latest_context_report(events: list[TraceEvent]) -> ContextReport:
    for event in reversed(events):
        if not isinstance(event.output_summary, dict):
            continue
        report = event.output_summary.get("context_report_v2")
        if isinstance(report, dict):
            return ContextReport.model_validate(report)
        report = event.output_summary.get("context_report_v1")
        if isinstance(report, dict):
            return context_report_v2_from_v1(report)
        context = event.output_summary.get("context")
        if isinstance(context, dict):
            nested_report = context.get("context_report_v2")
            if isinstance(nested_report, dict):
                return ContextReport.model_validate(nested_report)
            nested_report = context.get("context_report_v1")
            if isinstance(nested_report, dict):
                return context_report_v2_from_v1(nested_report)
    return context_report_from_trace_context_summary(_latest_context_summary(events))
