"""Convert Runtime trace events into compact Langfuse evaluator evidence."""

from collections.abc import Mapping
from typing import Any

from assistant_agent.observability.trace_store import TraceEvent


def tool_executions(events: list[TraceEvent]) -> list[dict[str, Any]]:
    terminals = {
        event.attributes.get("tool_call_id"): event
        for event in events
        if event.canonical_event in {"tool.finished", "tool.failed"}
    }
    executions: list[dict[str, Any]] = []
    exposed_tools: list[str] = []
    for event in events:
        if event.canonical_event == "context.build.finished":
            exposed_tools = _context_event_available_tools(event)
            continue
        if event.canonical_event != "tool.started":
            continue
        tool_call_id = event.attributes.get("tool_call_id")
        terminal = terminals.get(tool_call_id)
        output = terminal.output_summary if terminal is not None else {}
        executions.append(
            {
                "tool_call_id": tool_call_id,
                "name": event.tool_name,
                "input": event.input_summary,
                "status": terminal.status if terminal is not None else "missing_terminal",
                "terminal_event": (
                    terminal.canonical_event if terminal is not None else None
                ),
                "exposed": (
                    isinstance(event.tool_name, str)
                    and event.tool_name in exposed_tools
                ),
                "exposed_tools": list(exposed_tools),
                "outcome": _tool_outcome(output),
                "output": output,
                "error_code": (
                    terminal.error_code if terminal is not None else None
                ),
                "error": terminal.error if terminal is not None else None,
                "retry_count": (
                    terminal.attributes.get("retry_count")
                    if terminal is not None
                    else None
                ),
            }
        )
    return executions


def available_tools(state: Any, events: list[TraceEvent]) -> list[str]:
    available: list[str] = []
    run_tool_catalog = getattr(state, "run_tool_catalog", None)
    available_tool_names = getattr(run_tool_catalog, "available_tool_names", None)
    if isinstance(available_tool_names, list):
        _extend_unique_strings(available, available_tool_names)
    for event in events:
        _extend_unique_strings(available, _context_event_available_tools(event))
    return available


def validation_results(events: list[TraceEvent]) -> list[dict[str, Any]]:
    return [
        {
            "tool_name": event.tool_name,
            "status": event.status,
            "tool_call_id": event.attributes.get("tool_call_id"),
        }
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


def total_latency_ms(events: list[TraceEvent]) -> int:
    terminal = next(
        (
            event
            for event in reversed(events)
            if event.canonical_event
            in {"run.completed", "run.failed", "run.cancelled"}
        ),
        None,
    )
    return (
        terminal.latency_ms
        if terminal is not None and terminal.latency_ms is not None
        else 0
    )


def _context_event_available_tools(event: TraceEvent) -> list[str]:
    if event.canonical_event != "context.build.finished":
        return []
    report = event.output_summary.get("context_report_v1")
    if not isinstance(report, dict):
        return []
    selected = report.get("selected_tool_names")
    return (
        [name for name in selected if isinstance(name, str)]
        if isinstance(selected, list)
        else []
    )


def _extend_unique_strings(target: list[str], values: list[Any]) -> None:
    for value in values:
        if isinstance(value, str) and value not in target:
            target.append(value)


def _tool_outcome(output: Mapping[str, Any]) -> str | None:
    direct = output.get("outcome")
    if isinstance(direct, str):
        return direct
    data = output.get("data")
    if isinstance(data, Mapping) and isinstance(data.get("outcome"), str):
        return str(data["outcome"])
    return None
