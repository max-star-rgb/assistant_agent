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
    parser.add_argument("--json", dest="json_output", action="store_true", help="Print a JSON summary.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.include_conversation:
        if not args.server:
            parser.error("--include-conversation requires --server")
        if not _is_loopback_server(args.server):
            parser.error("--include-conversation requires a loopback --server URL")
    if args.server:
        payload = _fetch_server_trace(args.server, args.identifier)
        if payload is None:
            print(f"trace/run not found on server: {args.identifier}", file=sys.stderr)
            return 1
        if args.include_conversation:
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
        print(_format_human(payload, show_errors=args.errors))
        return 0

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
    turn_latency = payload.get("turn_latency")
    if isinstance(turn_latency, dict):
        lines.extend(("", *_format_turn_latency(turn_latency)))
    conversation = payload.get("conversation")
    if isinstance(conversation, dict):
        lines.extend(("", *_format_conversation(conversation)))
    if show_errors or isinstance(turn_latency, dict) or isinstance(conversation, dict):
        lines.extend(("", "Timeline"))
    for index, event in enumerate(events, start=1):
        lines.append(_format_event_line(index, event))
    return "\n".join(lines)


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
