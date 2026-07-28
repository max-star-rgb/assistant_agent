"""Project Runtime traces projected into stable eval evidence."""

from __future__ import annotations

from typing import Any

from assistant_agent.observability.trace_store import TraceEvent
from evals.agent.contracts import ToolExecution, ValidationResult


def tool_executions(events: list[TraceEvent]) -> list[ToolExecution]:
    terminals = {
        event.attributes.get("tool_call_id"): event
        for event in events
        if event.canonical_event in {"tool.finished", "tool.failed"}
    }
    exposed_tools = available_tools_from_events(events)
    executions: list[ToolExecution] = []
    for event in events:
        if event.canonical_event != "tool.started":
            continue
        tool_call_id = event.attributes.get("tool_call_id")
        terminal = terminals.get(tool_call_id)
        executions.append(
            ToolExecution(
                tool_call_id=(str(tool_call_id) if tool_call_id is not None else None),
                name=event.tool_name,
                input=(
                    dict(event.input_summary)
                    if isinstance(event.input_summary, dict)
                    else {}
                ),
                status=(
                    terminal.status
                    if terminal is not None and terminal.status
                    else "missing_terminal"
                ),
                terminal_event=(
                    terminal.canonical_event if terminal is not None else None
                ),
                exposed=event.tool_name in exposed_tools,
                error_code=(terminal.error_code if terminal is not None else None),
                output=(
                    dict(terminal.output_summary)
                    if terminal is not None
                    and isinstance(terminal.output_summary, dict)
                    else {}
                ),
            )
        )
    return executions


def available_tools(state: Any, events: list[TraceEvent]) -> list[str]:
    result: list[str] = []
    catalog = getattr(state, "run_tool_catalog", None)
    names = getattr(catalog, "available_tool_names", None)
    if isinstance(names, list):
        _extend_unique(result, names)
    _extend_unique(result, available_tools_from_events(events))
    return result


def available_tools_from_events(events: list[TraceEvent]) -> list[str]:
    result: list[str] = []
    for event in events:
        if event.canonical_event != "context.build.finished":
            continue
        report = event.output_summary.get("context_report_v1")
        selected = (
            report.get("selected_tool_names") if isinstance(report, dict) else None
        )
        if isinstance(selected, list):
            _extend_unique(result, selected)
    return result


def validation_results(events: list[TraceEvent]) -> list[ValidationResult]:
    return [
        ValidationResult(
            tool_name=event.tool_name,
            status=event.status,
            tool_call_id=(
                str(event.attributes.get("tool_call_id"))
                if event.attributes.get("tool_call_id") is not None
                else None
            ),
        )
        for event in events
        if event.canonical_event == "action.validation.finished"
    ]


def provider_result_kinds(events: list[TraceEvent]) -> list[str]:
    return [
        result_kind
        for event in events
        if event.canonical_event == "llm.chat.finished"
        and isinstance(
            result_kind := event.attributes.get("result_kind"),
            str,
        )
        and result_kind
    ]


def _extend_unique(target: list[str], values: list[Any]) -> None:
    for value in values:
        if isinstance(value, str) and value not in target:
            target.append(value)
