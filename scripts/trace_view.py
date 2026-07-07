#!/usr/bin/env python3
"""Inspect one redacted assistant run or trace from the local JSONL trace store."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from assistant_agent.services.trace_store import JsonlTraceStore, TraceEvent, trace_debug_summary


DEFAULT_TRACE_PATH = ".data/graph_trace.jsonl"
DETAIL_ATTRIBUTE_KEYS = (
    "decision_type",
    "tool_call_id",
    "iteration",
    "risk",
    "side_effect",
    "confirmation_state",
    "recovery_action",
    "retry_count",
    "budget_ratio",
    "context_usage_ratio",
    "cancel_source",
)
DETAIL_OUTPUT_KEYS = (
    "output_ref",
    "artifact_id",
    "artifact_ref",
    "item_count",
    "result_count",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="View a redacted assistant run or trace timeline.")
    parser.add_argument("identifier", help="Run id or trace id to inspect.")
    parser.add_argument(
        "--trace-path",
        default=DEFAULT_TRACE_PATH,
        help=f"JSONL trace store path. Defaults to {DEFAULT_TRACE_PATH}.",
    )
    parser.add_argument("--errors", action="store_true", help="Show error events before the full timeline.")
    parser.add_argument("--json", dest="json_output", action="store_true", help="Print a JSON summary.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = JsonlTraceStore(args.trace_path)
    events = _find_events(store, args.identifier)
    if not events:
        print(f"trace/run not found: {args.identifier}", file=sys.stderr)
        return 1

    payload = _summary_payload(events)
    if args.json_output:
        print(json.dumps(_json_ready(payload), ensure_ascii=False, indent=2))
        return 0

    print(_format_human(payload, show_errors=args.errors))
    return 0


def _find_events(store: JsonlTraceStore, identifier: str) -> list[TraceEvent]:
    by_run = store.list_by_run(identifier)
    if by_run:
        return by_run
    return store.list_by_trace(identifier)


def _summary_payload(events: list[TraceEvent]) -> dict[str, Any]:
    summary = trace_debug_summary(events)
    summary["status"] = _infer_status(summary["events"], summary["error_count"])
    summary["event_count"] = len(events)
    summary["duration_ms"] = _duration_ms(events)
    return summary


def _infer_status(events: list[dict[str, Any]], error_count: int) -> str:
    for event in reversed(events):
        canonical_event = event.get("canonical_event")
        if canonical_event == "run.completed":
            return "completed"
        if canonical_event == "run.failed":
            return "failed"
        if canonical_event == "run.cancelled":
            return "cancelled"
    for event in reversed(events):
        status = event.get("status")
        if isinstance(status, str) and status:
            return status
    return "failed" if error_count else "unknown"


def _duration_ms(events: list[TraceEvent]) -> int | None:
    if not events:
        return None
    started_at = events[0].created_at
    finished_at = events[-1].created_at
    return max(0, round((finished_at - started_at).total_seconds() * 1000))


def _format_human(payload: dict[str, Any], *, show_errors: bool) -> str:
    lines = [_format_header(payload)]
    events = payload.get("events", [])
    if show_errors:
        lines.append("")
        lines.append("Errors")
        error_events = _error_events(events)
        if error_events:
            for index, event in error_events:
                lines.append(_format_error_line(index, event))
        else:
            lines.append("  (none)")
        lines.append("")
        lines.append("Timeline")
    for index, event in enumerate(events, start=1):
        lines.append(_format_event_line(index, event))
    return "\n".join(lines)


def _format_header(payload: dict[str, Any]) -> str:
    duration = payload.get("duration_ms")
    duration_part = f" duration={duration}ms" if isinstance(duration, int) else ""
    return (
        f"run {payload.get('run_id')} trace {payload.get('trace_id')} "
        f"status={payload.get('status')} events={payload.get('event_count', 0)} "
        f"errors={payload.get('error_count', 0)}{duration_part}"
    )


def _format_event_line(index: int, event: dict[str, Any]) -> str:
    name = _event_name(event)
    details = _event_details(event)
    suffix = f" {' '.join(details)}" if details else ""
    return f"{index:02d}  {name:<34}{suffix}"


def _format_error_line(index: int, event: dict[str, Any]) -> str:
    name = _event_name(event)
    details = _event_details(event)
    message = event.get("error_message")
    if isinstance(message, str) and message:
        details.append(f"message={_compact_value(message)}")
    suffix = f" {' '.join(details)}" if details else ""
    return f"{index:02d}  {name:<34}{suffix}"


def _event_name(event: dict[str, Any]) -> str:
    name = event.get("canonical_event") or event.get("event_type") or event.get("node_name")
    return str(name or "event")


def _event_details(event: dict[str, Any]) -> list[str]:
    details: list[str] = []
    latency_ms = event.get("latency_ms")
    if isinstance(latency_ms, int):
        details.append(f"{latency_ms}ms")
    _append_named(details, "status", event.get("status"))
    _append_named(details, "tool", event.get("tool_name"))
    _append_named(details, "provider", event.get("provider"))
    _append_named(details, "model", event.get("model"))
    _append_named(details, "error", event.get("error_code"))
    _append_selected(details, event.get("output_summary"), DETAIL_OUTPUT_KEYS)
    _append_selected(details, event.get("attributes"), DETAIL_ATTRIBUTE_KEYS)
    return details


def _append_named(details: list[str], name: str, value: Any) -> None:
    if value is None or value == "":
        return
    details.append(f"{name}={_compact_value(value)}")


def _append_selected(details: list[str], values: Any, keys: tuple[str, ...]) -> None:
    if not isinstance(values, dict):
        return
    for key in keys:
        if key in values and values[key] not in (None, ""):
            details.append(f"{key}={_compact_value(values[key])}")


def _compact_value(value: Any, *, limit: int = 80) -> str:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    else:
        text = str(value)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _error_events(events: list[dict[str, Any]]) -> list[tuple[int, dict[str, Any]]]:
    return [
        (index, event)
        for index, event in enumerate(events, start=1)
        if event.get("error_code") or event.get("error_message")
    ]


def _json_ready(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if isinstance(value, tuple):
        return [_json_ready(item) for item in value]
    return value


if __name__ == "__main__":
    raise SystemExit(main())
