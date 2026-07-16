#!/usr/bin/env python3
"""Inspect one redacted assistant run or trace from the local JSONL trace store."""

# ruff: noqa: E402 - repository src path must be installed before package imports.

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import datetime
from ipaddress import ip_address
from pathlib import Path
from time import monotonic, sleep
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from assistant_agent.services.trace_store import TraceEvent, trace_debug_summary


DEFAULT_TRACE_PATH = ".data/graph_trace.jsonl"
LATEST_IDENTIFIERS = {"last", "latest", "@last"}
TRACE_SECTION_ORDER = ("conversation", "timeline", "react")
TRACE_SECTION_ALIASES = {
    "all": TRACE_SECTION_ORDER,
    "full": TRACE_SECTION_ORDER,
}
REACT_DETAIL_EVENTS = {
    "llm.chat.finished",
    "react.decision",
    "action.validation.finished",
    "tool.started",
    "tool.finished",
    "tool.failed",
    "tool.observation",
    "loop_guard.triggered",
}
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
    "terminal_status",
    "runtime_call_latency_ms",
    "provider_latency_ms",
    "wall_latency_ms",
    "gateway_run_id",
    "assistant_run_id",
    "result_run_id",
    "tool_count",
    "error_count",
    "response_present",
    "user_visible_event_count",
    "sla_fallback_emitted",
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
        "--server",
        help=(
            "Assistant server base URL, for example http://127.0.0.1:8000. "
            "When set, query the running server instead of the local JSONL trace file."
        ),
    )
    parser.add_argument(
        "--trace-path",
        default=DEFAULT_TRACE_PATH,
        help=f"Local JSONL trace store path. Ignored when --server is set. Defaults to {DEFAULT_TRACE_PATH}.",
    )
    parser.add_argument("--errors", action="store_true", help="Show error events before the full timeline.")
    parser.add_argument(
        "--include-conversation",
        action="store_true",
        help="Fetch current-turn content from an explicitly enabled loopback server.",
    )
    parser.add_argument(
        "--sections",
        help=(
            "Comma-separated output sections: conversation,timeline,react. "
            "Use full/all for all sections. Defaults to timeline."
        ),
    )
    parser.add_argument(
        "--react-detail",
        action="store_true",
        help="Add a ReAct detail section with prompt-safe decision/tool evidence.",
    )
    parser.add_argument("--json", dest="json_output", action="store_true", help="Print a JSON summary.")
    parser.add_argument(
        "--follow",
        action="store_true",
        help="Watch the local trace file and print updates until interrupted.",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        help="Seconds between local --follow polls. Defaults to 1.0.",
    )
    parser.add_argument(
        "--follow-limit",
        type=int,
        help="Stop after this many printed updates. Mainly useful for tests and scripted checks.",
    )
    parser.add_argument(
        "--follow-timeout",
        type=float,
        help="Stop following after this many seconds. By default --follow runs until interrupted.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.follow and args.server:
        parser.error("--follow requires a local --trace-path")
    if args.follow and args.json_output:
        parser.error("--json cannot be combined with --follow")
    if args.follow and args.poll_interval <= 0:
        parser.error("--poll-interval must be greater than 0")
    if args.follow_limit is not None and args.follow_limit <= 0:
        parser.error("--follow-limit must be greater than 0")
    if args.follow_timeout is not None and args.follow_timeout < 0:
        parser.error("--follow-timeout must be greater than or equal to 0")
    sections = _parse_sections(parser, args.sections, include_conversation=args.include_conversation, react_detail=args.react_detail)
    include_conversation = args.include_conversation or "conversation" in sections
    if include_conversation:
        if not args.server:
            parser.error("conversation output requires --server")
        if not _is_loopback_server(args.server):
            parser.error("conversation output requires a loopback --server URL")
    if "conversation" in sections and args.follow:
        parser.error("conversation output cannot be combined with --follow")
    if args.server:
        identifier = args.identifier
        local_events: list[TraceEvent] = []
        if _is_latest_identifier(identifier):
            local_events = _find_local_events(args.trace_path, identifier)
            if not local_events:
                print(f"trace/run not found: {identifier}", file=sys.stderr)
                return 1
            identifier = local_events[0].trace_id
        payload = _fetch_server_trace(args.server, identifier)
        if payload is None and local_events:
            payload = _summary_payload(local_events)
        if payload is None:
            print(f"trace/run not found on server: {args.identifier}", file=sys.stderr)
            return 1
        if include_conversation:
            trace_id = payload.get("trace_id")
            if not isinstance(trace_id, str) or not trace_id:
                print("trace id is unavailable for conversation lookup", file=sys.stderr)
                return 1
            conversation = _get_server_json(
                args.server,
                f"/traces/{quote(trace_id, safe='')}/conversation",
            )
            if conversation is None:
                print(f"conversation not found for trace: {trace_id}", file=sys.stderr)
                return 1
            payload["conversation"] = conversation
        payload = _server_summary_payload(payload)
        if args.json_output:
            print(json.dumps(_json_ready(payload), ensure_ascii=False, indent=2))
            return 0
        print(_format_human(payload, show_errors=args.errors, sections=sections))
        return 0

    if args.follow:
        return _follow_local_trace(args)

    events = _find_local_events(args.trace_path, args.identifier)
    if not events:
        print(f"trace/run not found: {args.identifier}", file=sys.stderr)
        return 1

    payload = _summary_payload(events)
    if args.json_output:
        print(json.dumps(_json_ready(payload), ensure_ascii=False, indent=2))
        return 0

    print(_format_human(payload, show_errors=args.errors, sections=sections))
    return 0


def _parse_sections(
    parser: argparse.ArgumentParser,
    value: str | None,
    *,
    include_conversation: bool,
    react_detail: bool,
) -> tuple[str, ...]:
    requested: list[str]
    if value is None:
        requested = ["timeline"]
        if include_conversation:
            requested.append("conversation")
        if react_detail:
            requested.append("react")
    else:
        requested = []
        for raw_item in value.split(","):
            item = raw_item.strip().lower()
            if not item:
                continue
            if item in TRACE_SECTION_ALIASES:
                requested.extend(TRACE_SECTION_ALIASES[item])
            elif item in TRACE_SECTION_ORDER:
                requested.append(item)
            else:
                parser.error(
                    "--sections must contain only conversation,timeline,react,full,all"
                )
    if not requested:
        parser.error("--sections must not be empty")
    return tuple(section for section in TRACE_SECTION_ORDER if section in set(requested))


def _is_latest_identifier(identifier: str) -> bool:
    return identifier.lower() in LATEST_IDENTIFIERS


def _find_local_events(trace_path: str | Path, identifier: str) -> list[TraceEvent]:
    events = _load_local_events(trace_path)
    if _is_latest_identifier(identifier):
        return _latest_run_events(events)
    return _events_for_identifier(events, identifier)


def _load_local_events(trace_path: str | Path) -> list[TraceEvent]:
    path = Path(trace_path)
    if not path.exists():
        return []
    events: list[TraceEvent] = []
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            if line.strip():
                events.append(TraceEvent.model_validate_json(line))
    return events


def _latest_run_events(events: list[TraceEvent]) -> list[TraceEvent]:
    if not events:
        return []
    latest = events[-1]
    return [event for event in events if event.run_id == latest.run_id]


def _events_for_identifier(events: list[TraceEvent], identifier: str) -> list[TraceEvent]:
    by_run = [event for event in events if event.run_id == identifier]
    if by_run:
        return by_run
    return [event for event in events if event.trace_id == identifier]


def _follow_local_trace(args: argparse.Namespace) -> int:
    deadline = None if args.follow_timeout is None else monotonic() + args.follow_timeout
    previous_signature: tuple[tuple[Any, ...], ...] | None = None
    printed_any = False
    printed_updates = 0
    sections = _parse_sections(
        build_parser(),
        args.sections,
        include_conversation=args.include_conversation,
        react_detail=args.react_detail,
    )
    while True:
        events = _find_local_events(args.trace_path, args.identifier)
        if events:
            signature = _events_signature(events)
            if signature != previous_signature:
                if printed_any:
                    print()
                    print("--- trace update ---")
                print(_format_human(_summary_payload(events), show_errors=args.errors, sections=sections), flush=True)
                previous_signature = signature
                printed_any = True
                printed_updates += 1
                if args.follow_limit is not None and printed_updates >= args.follow_limit:
                    return 0
        if deadline is not None and monotonic() >= deadline:
            if printed_any:
                return 0
            print(f"trace/run not found: {args.identifier}", file=sys.stderr)
            return 1
        sleep(args.poll_interval)


def _events_signature(events: list[TraceEvent]) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        (
            event.trace_id,
            event.run_id,
            event.canonical_event,
            event.event_type,
            event.status,
            event.tool_name,
            event.provider,
            event.model,
            event.latency_ms,
            event.error_code,
            event.created_at.isoformat(),
        )
        for event in events
    )


def _fetch_server_trace(server: str, identifier: str) -> dict[str, Any] | None:
    trace_payload = _get_server_json(server, f"/traces/{quote(identifier, safe='')}")
    if trace_payload is not None:
        return trace_payload

    run_payload = _get_server_json(server, f"/runs/{quote(identifier, safe='')}")
    if run_payload is None:
        return None
    trace_id = run_payload.get("trace_id")
    if isinstance(trace_id, str) and trace_id:
        trace_payload = _get_server_json(server, f"/traces/{quote(trace_id, safe='')}")
        if trace_payload is not None:
            return trace_payload
    return run_payload


def _get_server_json(server: str, path: str) -> dict[str, Any] | None:
    url = f"{server.rstrip('/')}{path}"
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=10) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise SystemExit(f"server request failed: HTTP {exc.code} {url}") from exc
    except URLError as exc:
        raise SystemExit(f"server request failed: {exc.reason}") from exc
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"server returned invalid JSON: {url}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"server returned non-object JSON: {url}")
    return payload


def _is_loopback_server(server: str) -> bool:
    hostname = urlparse(server).hostname
    if not hostname:
        return False
    if hostname == "localhost":
        return True
    try:
        return ip_address(hostname).is_loopback
    except ValueError:
        return False


def _server_summary_payload(payload: dict[str, Any]) -> dict[str, Any]:
    summary = dict(payload)
    events = summary.get("events")
    if not isinstance(events, list):
        events = []
        summary["events"] = events
    _add_timing_fields(events)
    error_count = summary.get("error_count")
    if not isinstance(error_count, int):
        error_count = len(_error_events(events))
        summary["error_count"] = error_count
    if not isinstance(summary.get("event_count"), int):
        summary["event_count"] = len(events)
    if not isinstance(summary.get("status"), str):
        summary["status"] = _infer_status(events, error_count)
    return summary


def _summary_payload(events: list[TraceEvent]) -> dict[str, Any]:
    summary = trace_debug_summary(events)
    _add_timing_fields(summary["events"])
    summary["status"] = _infer_status(summary["events"], summary["error_count"])
    summary["event_count"] = len(events)
    summary["duration_ms"] = _duration_ms(events)
    return summary


def _add_timing_fields(events: list[dict[str, Any]]) -> None:
    if not events:
        return
    started_at = _event_created_at(events[0])
    previous_at = started_at
    for event in events:
        current_at = _event_created_at(event)
        if started_at is not None and current_at is not None:
            event["elapsed_ms"] = max(0, round((current_at - started_at).total_seconds() * 1000))
        if previous_at is not None and current_at is not None and current_at is not previous_at:
            event["gap_ms"] = max(0, round((current_at - previous_at).total_seconds() * 1000))
        previous_at = current_at or previous_at


def _event_created_at(event: dict[str, Any]) -> datetime | None:
    value = event.get("created_at")
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


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


def _format_human(payload: dict[str, Any], *, show_errors: bool, sections: tuple[str, ...] = ("timeline",)) -> str:
    lines = [_format_header(payload)]
    events = payload.get("events", [])
    conversation = payload.get("conversation")
    if "conversation" in sections and isinstance(conversation, dict):
        lines.extend(("", *_format_conversation(conversation)))
    if show_errors:
        lines.append("")
        lines.append("Errors")
        error_events = _error_events(events)
        if error_events:
            for index, event in error_events:
                lines.append(_format_error_line(index, event))
        else:
            lines.append("  (none)")
    if "timeline" in sections:
        turn_latency = payload.get("turn_latency")
        if isinstance(turn_latency, dict):
            lines.extend(("", *_format_turn_latency(turn_latency)))
        if show_errors or isinstance(turn_latency, dict) or ("conversation" in sections and isinstance(conversation, dict)):
            lines.extend(("", "Timeline"))
        for index, event in enumerate(events, start=1):
            lines.append(_format_event_line(index, event))
    if "react" in sections:
        lines.extend(("", *_format_react_detail(events)))
    return "\n".join(lines)


def _format_react_detail(events: list[dict[str, Any]]) -> list[str]:
    lines = ["ReAct detail"]
    react_events = [
        (index, event)
        for index, event in enumerate(events, start=1)
        if _event_name(event) in REACT_DETAIL_EVENTS
    ]
    if not react_events:
        lines.append("  (none)")
        return lines
    for index, event in react_events:
        details = _react_event_details(event)
        suffix = f" {' '.join(details)}" if details else ""
        lines.append(f"  {index:02d} {_event_name(event)}{suffix}")
    return lines


def _react_event_details(event: dict[str, Any]) -> list[str]:
    name = _event_name(event)
    details: list[str] = []
    attributes = event.get("attributes")
    output_summary = event.get("output_summary")
    if isinstance(attributes, dict):
        _append_named(details, "iteration", attributes.get("iteration"))
        _append_named(details, "batch", _batch_value(attributes))
    latency_ms = event.get("latency_ms")
    if isinstance(latency_ms, int):
        details.append(f"latency={latency_ms}ms")
    _append_named(details, "status", event.get("status"))
    _append_named(details, "tool", event.get("tool_name"))
    _append_named(details, "provider", event.get("provider"))
    _append_named(details, "model", event.get("model"))
    if name == "react.decision" and isinstance(output_summary, dict):
        _append_named(details, "decision", output_summary.get("decision_type") or event.get("status"))
        _append_named(details, "why", output_summary.get("reason"))
        _append_named(details, "confidence", output_summary.get("confidence"))
        _append_context_evidence(details, output_summary.get("context"))
    elif name == "action.validation.finished":
        _append_named(details, "validation", event.get("status"))
        if isinstance(output_summary, dict):
            validator = output_summary.get("validator_result")
            if isinstance(validator, dict):
                _append_named(details, "code", validator.get("code"))
                _append_named(details, "message", validator.get("message"))
        if isinstance(attributes, dict):
            _append_named(details, "risk", attributes.get("risk"))
            _append_named(details, "side_effect", attributes.get("side_effect"))
            _append_named(details, "confirmation", attributes.get("confirmation_state"))
    elif name.startswith("tool."):
        _append_named(details, "error", event.get("error_code"))
        if isinstance(attributes, dict):
            _append_named(details, "tool_call_id", attributes.get("tool_call_id"))
            _append_named(details, "recovery", attributes.get("recovery_action"))
        if isinstance(output_summary, dict):
            _append_named(details, "output_ref", output_summary.get("output_ref"))
            _append_named(details, "artifact", output_summary.get("artifact_ref") or output_summary.get("artifact_id"))
            _append_named(details, "results", output_summary.get("result_count") or output_summary.get("item_count"))
    else:
        _append_named(details, "error", event.get("error_code"))
    return details


def _batch_value(attributes: dict[str, Any]) -> str | None:
    index = attributes.get("batch_index")
    size = attributes.get("batch_size")
    if isinstance(index, int) and isinstance(size, int):
        return f"{index}/{size}"
    return None


def _append_context_evidence(details: list[str], context: Any) -> None:
    if not isinstance(context, dict):
        return
    budget = context.get("budget")
    if isinstance(budget, dict):
        _append_named(details, "context_usage", budget.get("context_usage_ratio"))
        total = budget.get("total_tokens")
        maximum = budget.get("max_tokens")
        if isinstance(total, int) and isinstance(maximum, int):
            details.append(f"context_tokens={total}/{maximum}")
    source_counts = context.get("source_counts")
    if isinstance(source_counts, dict):
        compact_counts = {
            key: value
            for key, value in source_counts.items()
            if key in {"conversation_turns", "memory_items", "tool_observations", "realtime_video_context"}
        }
        if compact_counts:
            details.append(f"sources={_compact_value(compact_counts)}")


def _format_turn_latency(summary: dict[str, Any]) -> list[str]:
    status = _plain_value(summary.get("status"))
    delivery = _plain_value(summary.get("delivery_id"))
    session_turn = _plain_value(summary.get("session_turn"))
    total = _milliseconds(summary.get("total_ms"))
    lines = [
        "Turn latency",
        f"  status={status} delivery={delivery} session_turn={session_turn} total={total}",
        "  "
        f"trace={_plain_value(summary.get('trace_id'))} "
        f"gateway_run={_plain_value(summary.get('gateway_run_id'))} "
        f"assistant_run={_plain_value(summary.get('assistant_run_id'))}",
    ]
    bottleneck = summary.get("bottleneck")
    if bottleneck:
        share = summary.get("bottleneck_share_pct")
        share_text = f" ({share}%)" if isinstance(share, (int, float)) else ""
        lines.append(
            f"  bottleneck={_plain_value(bottleneck)} "
            f"{_milliseconds(summary.get('bottleneck_ms'))}{share_text}"
        )

    stages = summary.get("stages")
    stage_items = [item for item in stages if isinstance(item, dict)] if isinstance(stages, list) else []
    unattributed_ms = summary.get("unattributed_ms")
    if isinstance(unattributed_ms, int) and unattributed_ms > 0 and not any(
        item.get("name") == "unattributed" for item in stage_items
    ):
        stage_items.append({"name": "unattributed", "duration_ms": unattributed_ms})
    if stage_items:
        lines.append("  Stages")
        lines.extend(_format_latency_stage(item) for item in stage_items)

    ack_status = _plain_value(summary.get("ack_status"))
    ack_latency = summary.get("ack_latency_ms")
    ack_suffix = f" {_milliseconds(ack_latency)}" if isinstance(ack_latency, int) else ""
    lines.append(f"  ACK: {ack_status}{ack_suffix}")
    video = summary.get("video")
    if isinstance(video, dict):
        lines.append(_format_video_latency(video))
    return lines


def _format_latency_stage(stage: dict[str, Any]) -> str:
    details = [
        f"    {_plain_value(stage.get('name'))}",
        _milliseconds(stage.get("duration_ms")),
    ]
    for key in ("iteration", "tool_name", "provider", "model"):
        value = stage.get(key)
        if value not in (None, ""):
            details.append(f"{key}={_plain_value(value)}")
    provider_latency = stage.get("provider_latency_ms")
    if isinstance(provider_latency, int):
        details.append(f"provider_latency={provider_latency}ms")
    return " ".join(details)


def _format_video_latency(video: dict[str, Any]) -> str:
    details = ["  Video:"]
    keys = (
        ("source", "source", False),
        ("snapshot_age_ms", "snapshot_age", True),
        ("observation_latency_ms", "observation_latency", True),
        ("pending_count", "pending", False),
        ("in_flight", "in_flight", False),
        ("fallback_used", "fallback", False),
        ("snapshot_sequence", "sequence", False),
        ("provider", "provider", False),
        ("model", "model", False),
    )
    for source_key, output_key, milliseconds in keys:
        value = video.get(source_key)
        if value is None:
            continue
        rendered = _milliseconds(value) if milliseconds else _plain_value(value)
        details.append(f"{output_key}={rendered}")
    return " ".join(details)


def _format_conversation(conversation: dict[str, Any]) -> list[str]:
    return [
        "Conversation",
        _format_conversation_side("User", conversation.get("user")),
        _format_conversation_side("Assistant", conversation.get("assistant")),
    ]


def _format_conversation_side(label: str, value: Any) -> str:
    if not isinstance(value, dict):
        return f"  {label}: (unavailable)"
    text = value.get("text") if isinstance(value.get("text"), str) else ""
    suffix = ""
    if value.get("truncated") is True:
        suffix = f" [truncated from {_plain_value(value.get('chars'))} chars]"
    return f"  {label}: {text}{suffix}"


def _plain_value(value: Any) -> str:
    if value is None or value == "":
        return "none"
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def _milliseconds(value: Any) -> str:
    return f"{value}ms" if isinstance(value, int) else "none"


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
    elapsed_ms = event.get("elapsed_ms")
    if isinstance(elapsed_ms, int):
        details.append(f"at={elapsed_ms}ms")
    gap_ms = event.get("gap_ms")
    if isinstance(gap_ms, int) and gap_ms > 0:
        details.append(f"gap={gap_ms}ms")
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
