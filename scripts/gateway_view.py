#!/usr/bin/env python3
"""Render prompt-safe Gateway lifecycle logs as a developer timeline."""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import sys
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep
from typing import Any


DEFAULT_GATEWAY_EVENT_PATH = ".data/gateway_events.jsonl"
DEFAULT_TAIL_LINES = 50
DEFAULT_POLL_INTERVAL = 1.0
RAW_LINE_LIMIT = 240
LATEST_IDENTIFIERS = {"last", "latest", "@last"}
FOLLOW_SESSION_SEPARATOR = "=" * 16
GATEWAY_TERMINAL_EVENTS = frozenset(
    {
        "gateway.run.completed",
        "gateway.run.cancelled",
        "gateway.run.errored",
        "gateway.run.queue_rejected",
        "gateway.run.queue_expired",
    }
)

MACHINE_KEYS = {
    "level",
    "component",
    "schema_version",
    "event",
    "run_id",
    "turn_id",
    "trace_id",
    "created_at",
    "message",
}
IDENTIFIER_KEYS = {
    "run_id": "run",
    "turn_id": "turn",
    "trace_id": "trace",
}
PAYLOAD_KEY_LABELS = {
    "user_id": "user",
    "session_id": "session",
}
EVENT_LABELS = {
    "gateway.server.starting": "server starting",
    "gateway.session.acquired": "session acquired",
    "gateway.session.destroyed": "session destroyed",
    "gateway.session.hangup_marked": "session hangup marked",
    "gateway.run.queued": "run queued",
    "gateway.run.queue_rejected": "run queue rejected",
    "gateway.run.queue_expired": "run queue expired",
    "gateway.run.admitted": "run admitted",
    "gateway.run.started": "run started",
    "gateway.run.completed": "run completed",
    "gateway.run.cancel_requested": "cancel requested",
    "gateway.run.cancelled": "run cancelled",
    "gateway.run.errored": "run errored",
}
EVENT_DETAIL_KEYS = {
    "gateway.server.starting": ("host", "port", "log_dir"),
    "gateway.session.acquired": ("created", "resumed", "user_id", "session_id"),
    "gateway.session.destroyed": ("reason", "user_id", "session_id"),
    "gateway.session.hangup_marked": ("reason", "newly_marked", "user_id", "session_id"),
    "gateway.run.queued": (
        "queue_reason",
        "queue_depth",
        "global_queue_depth",
        "limit",
        "user_id",
        "session_id",
    ),
    "gateway.run.queue_rejected": (
        "reason",
        "queue_reason",
        "queue_depth",
        "global_queue_depth",
        "limit",
        "user_id",
        "session_id",
    ),
    "gateway.run.queue_expired": (
        "reason",
        "queue_wait_ms",
        "queue_depth",
        "global_queue_depth",
        "user_id",
        "session_id",
    ),
    "gateway.run.admitted": (
        "active_count",
        "active_runs",
        "max_active_runs",
        "queue_depth",
        "global_queue_depth",
        "user_id",
        "session_id",
    ),
    "gateway.run.started": (
        "queue_depth",
        "global_queue_depth",
        "queue_wait_ms",
        "expects_reply",
        "user_id",
        "session_id",
    ),
    "gateway.run.completed": ("status", "reason", "user_id", "session_id"),
    "gateway.run.cancel_requested": (
        "source",
        "phase",
        "cancel_phase",
        "reason",
        "handled_by",
        "user_id",
        "session_id",
    ),
    "gateway.run.cancelled": (
        "source",
        "phase",
        "cancel_phase",
        "reason",
        "user_id",
        "session_id",
    ),
    "gateway.run.errored": ("status", "reason", "user_id", "session_id"),
}


@dataclass(frozen=True)
class GatewayLogEntry:
    raw: str
    timestamp: str
    fields: dict[str, Any]
    parsed: bool


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="View Gateway lifecycle events as a timeline.")
    parser.add_argument("identifier", nargs="?", default="last", help="Run id, trace id, or last/latest/@last.")
    parser.add_argument(
        "--event-path",
        default=DEFAULT_GATEWAY_EVENT_PATH,
        help=f"Gateway lifecycle JSONL path. Defaults to {DEFAULT_GATEWAY_EVENT_PATH}.",
    )
    parser.add_argument(
        "--log-path",
        help="Legacy Gateway key=value log path. When set, --event-path is ignored.",
    )
    parser.add_argument(
        "--session-id",
        help="Filter Gateway JSONL lookup to one raw session id.",
    )
    parser.add_argument(
        "--tail",
        type=int,
        default=None,
        help=f"Print the last N raw events instead of resolving an identifier. Typical value: {DEFAULT_TAIL_LINES}.",
    )
    parser.add_argument(
        "--follow",
        action="store_true",
        help="Watch the local Gateway event file and print matching updates until interrupted.",
    )
    parser.add_argument(
        "--follow-include-existing",
        action="store_true",
        help="With last/latest/@last --follow, print the currently matching Gateway run before waiting.",
    )
    parser.add_argument(
        "--follow-live-updates",
        action="store_true",
        help="Print non-terminal Gateway run updates while a run is still in progress.",
    )
    parser.add_argument(
        "--follow-all-sessions",
        action="store_true",
        help=(
            "Compatibility flag. last/latest/@last --follow already watches the global latest stream; "
            "use --session-id to isolate one session."
        ),
    )
    parser.add_argument(
        "--show-session-banner",
        action="store_true",
        help="Compatibility flag. --follow already prints SESSION banners for each observed session.",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL,
        help=f"Seconds between --follow polls. Defaults to {DEFAULT_POLL_INTERVAL}.",
    )
    parser.add_argument(
        "--follow-limit",
        type=int,
        help="Stop after this many rendered log entries. Mainly useful for tests and scripted checks.",
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
    if args.follow and args.log_path and args.session_id:
        parser.error("--session-id is not supported with legacy --log-path follow mode")
    if args.follow and args.tail is not None:
        parser.error("--tail cannot be combined with --follow; use --follow-include-existing to print current latest before following")
    if args.tail is not None and args.tail < 0:
        parser.error("--tail must be greater than or equal to 0")
    if args.poll_interval <= 0:
        parser.error("--poll-interval must be greater than 0")
    if args.follow_limit is not None and args.follow_limit <= 0:
        parser.error("--follow-limit must be greater than 0")
    if args.follow_timeout is not None and args.follow_timeout < 0:
        parser.error("--follow-timeout must be greater than or equal to 0")

    source_path, source_format, line_parser = _source(args)
    if args.follow:
        return _follow_gateway_events(args, source_path, line_parser=line_parser)
    if args.tail is not None:
        return _print_tail_view(source_path, source_format, args.tail, line_parser=line_parser)
    events = _find_gateway_events(
        source_path,
        args.identifier,
        line_parser=line_parser,
        session_id=args.session_id,
    )
    if not events:
        print(f"gateway run/event not found: {args.identifier}", file=sys.stderr)
        return 1
    print(_format_gateway_payload(events), flush=True)
    return 0


def _print_tail_view(
    source_path: Path,
    source_format: str,
    tail: int,
    *,
    line_parser: Callable[[str], GatewayLogEntry | None],
) -> int:
    print(
        _format_header(source_path, source_format=source_format, follow=False, tail=tail),
        flush=True,
    )
    printed_entries = _print_tail(source_path, tail, line_parser=line_parser)
    if printed_entries == 0:
        print("  (no gateway log entries)", flush=True)
    return 0


def _source(args: argparse.Namespace) -> tuple[Path, str, Callable[[str], GatewayLogEntry | None]]:
    if args.log_path:
        source_path = Path(args.log_path)
        source_format = "key-value"
        line_parser = _parse_key_value_log_line
    else:
        source_path = Path(args.event_path)
        source_format = "jsonl"
        line_parser = _parse_jsonl_event_line
    if not source_path.is_absolute():
        source_path = Path.cwd() / source_path
    return source_path, source_format, line_parser


def _format_header(source_path: Path, *, source_format: str, follow: bool, tail: int) -> str:
    mode = "follow" if follow else "tail"
    return f"Gateway timeline path={source_path} source={source_format} mode={mode} tail={tail}"


def _print_tail(
    source_path: Path,
    tail: int,
    *,
    line_parser: Callable[[str], GatewayLogEntry | None],
) -> int:
    if tail == 0:
        return 0
    if not source_path.exists():
        return 0
    lines = _tail_lines(source_path, tail)
    return _print_entries(lines, line_parser=line_parser)


def _tail_lines(source_path: Path, tail: int) -> list[str]:
    window: deque[str] = deque(maxlen=tail)
    with source_path.open("r", encoding="utf-8") as file:
        for line in file:
            if line.strip():
                window.append(line)
    return list(window)


def _find_gateway_events(
    source_path: str | Path,
    identifier: str,
    *,
    line_parser: Callable[[str], GatewayLogEntry | None],
    session_id: str | None = None,
) -> list[GatewayLogEntry]:
    entries = _load_gateway_entries(source_path, line_parser=line_parser)
    if session_id:
        expected_session_id = _session_id_filter_value(session_id)
        entries = [
            entry
            for entry in entries
            if str(entry.fields.get("session_id") or "") == expected_session_id
        ]
    if _is_latest_identifier(identifier):
        return _latest_gateway_events(entries)
    return _gateway_events_for_identifier(entries, identifier)


def _load_gateway_entries(
    source_path: str | Path,
    *,
    line_parser: Callable[[str], GatewayLogEntry | None],
) -> list[GatewayLogEntry]:
    path = Path(source_path)
    if not path.exists():
        return []
    entries: list[GatewayLogEntry] = []
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            if not line.strip():
                continue
            entry = line_parser(line)
            if entry is not None and entry.parsed:
                entries.append(entry)
    return entries


def _is_latest_identifier(identifier: str) -> bool:
    return identifier.lower() in LATEST_IDENTIFIERS


def _latest_gateway_events(entries: list[GatewayLogEntry]) -> list[GatewayLogEntry]:
    if not entries:
        return []
    latest = _latest_gateway_anchor(entries)
    run_id = _entry_run_id(latest)
    if run_id:
        return [entry for entry in entries if _entry_run_id(entry) == run_id]
    trace_id = _entry_trace_id(latest)
    if trace_id:
        return [entry for entry in entries if _entry_trace_id(entry) == trace_id]
    return [latest]


def _latest_gateway_anchor(entries: list[GatewayLogEntry]) -> GatewayLogEntry:
    for entry in reversed(entries):
        if _entry_run_id(entry) or _entry_trace_id(entry):
            return entry
    return entries[-1]


def _gateway_events_for_identifier(entries: list[GatewayLogEntry], identifier: str) -> list[GatewayLogEntry]:
    by_run = [entry for entry in entries if _entry_run_id(entry) == identifier]
    if by_run:
        return by_run
    by_trace = [entry for entry in entries if _entry_trace_id(entry) == identifier]
    if by_trace:
        return by_trace
    return [entry for entry in entries if _entry_event_name(entry) == identifier]


def _session_id_filter_value(value: str) -> str:
    if value.startswith("sha256:"):
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"sha256:{digest}"


def _follow_gateway_events(
    args: argparse.Namespace,
    source_path: Path,
    *,
    line_parser: Callable[[str], GatewayLogEntry | None],
) -> int:
    deadline = None if args.follow_timeout is None else monotonic() + args.follow_timeout
    suppressed_group_ids = _initial_suppressed_follow_group_ids(
        args,
        source_path,
        line_parser=line_parser,
    )
    previous_session_id: str | None = None
    printed_any = False
    printed_updates = 0
    printed_group_ids: set[str] = set()
    printed_signatures: dict[str, tuple[tuple[Any, ...], ...]] = {}
    saw_matching_events = bool(
        _follow_event_groups(
            args,
            source_path,
            line_parser=line_parser,
            suppressed_group_ids=set(),
        )
    )
    locked_session_id: str | None = None
    while True:
        event_groups = _follow_event_groups(
            args,
            source_path,
            line_parser=line_parser,
            suppressed_group_ids=suppressed_group_ids,
            locked_session_id=locked_session_id,
        )
        if event_groups:
            saw_matching_events = True
        for events in event_groups:
            signature = _events_signature(events)
            group_id = _follow_group_id(events)
            if not args.follow_live_updates and group_id in printed_group_ids:
                continue
            if args.follow_live_updates and printed_signatures.get(group_id) == signature:
                continue
            if not args.follow_live_updates and not _follow_update_ready(events):
                continue
            current_session_id = _gateway_session_id(events)
            session_changed = args.session_id is None and current_session_id != previous_session_id
            if session_changed:
                previous_session_id = current_session_id
            locked_session_id = _next_locked_follow_session_id(
                args,
                locked_session_id=locked_session_id,
                current_session_id=current_session_id,
            )
            if session_changed and _should_print_session_separator(
                args,
                printed_any=printed_any,
                session_changed=session_changed,
            ):
                print()
                print(_format_follow_session_separator(current_session_id))
            elif printed_any:
                print()
                print("--- gateway update ---")
            print(_format_gateway_payload(events), flush=True)
            printed_signatures[group_id] = signature
            printed_any = True
            if not args.follow_live_updates:
                printed_group_ids.add(group_id)
            printed_updates += 1
            if args.follow_limit is not None and printed_updates >= args.follow_limit:
                return 0
        if deadline is not None and monotonic() >= deadline:
            if printed_any:
                return 0
            if saw_matching_events:
                return 0
            print(f"gateway run/event not found: {args.identifier}", file=sys.stderr)
            return 1
        sleep(args.poll_interval)


def _initial_suppressed_follow_group_ids(
    args: argparse.Namespace,
    source_path: Path,
    *,
    line_parser: Callable[[str], GatewayLogEntry | None],
) -> set[str]:
    if not _is_latest_identifier(args.identifier):
        return set()
    entries = _load_gateway_entries(source_path, line_parser=line_parser)
    lookup_session_id = _follow_lookup_session_id(args, locked_session_id=None)
    if lookup_session_id:
        expected_session_id = _session_id_filter_value(lookup_session_id)
        entries = [
            entry
            for entry in entries
            if str(entry.fields.get("session_id") or "") == expected_session_id
        ]
    groups = _follow_latest_gateway_groups(entries)
    if not args.follow_include_existing:
        return {group_id for group_id, _ in groups}
    latest_events = _latest_gateway_events(entries)
    latest_group_id = _follow_group_id(latest_events) if latest_events else None
    return {group_id for group_id, _ in groups if group_id != latest_group_id}


def _follow_event_groups(
    args: argparse.Namespace,
    source_path: Path,
    *,
    line_parser: Callable[[str], GatewayLogEntry | None],
    suppressed_group_ids: set[str],
    locked_session_id: str | None = None,
) -> list[list[GatewayLogEntry]]:
    lookup_session_id = _follow_lookup_session_id(args, locked_session_id=locked_session_id)
    if not _is_latest_identifier(args.identifier):
        events = _find_gateway_events(
            source_path,
            args.identifier,
            line_parser=line_parser,
            session_id=lookup_session_id,
        )
        if not events or _follow_group_id(events) in suppressed_group_ids:
            return []
        return [events]
    entries = _load_gateway_entries(source_path, line_parser=line_parser)
    if lookup_session_id:
        expected_session_id = _session_id_filter_value(lookup_session_id)
        entries = [
            entry
            for entry in entries
            if str(entry.fields.get("session_id") or "") == expected_session_id
        ]
    return [
        group
        for group_id, group in _follow_latest_gateway_groups(entries)
        if group_id not in suppressed_group_ids
    ]


def _follow_latest_gateway_groups(entries: list[GatewayLogEntry]) -> list[tuple[str, list[GatewayLogEntry]]]:
    return [
        (group_id, group)
        for group_id, group in _group_gateway_events(entries)
        if _gateway_group_has_run_or_trace(group)
    ]


def _gateway_group_has_run_or_trace(entries: list[GatewayLogEntry]) -> bool:
    return any(_entry_run_id(entry) or _entry_trace_id(entry) for entry in entries)


def _group_gateway_events(entries: list[GatewayLogEntry]) -> list[tuple[str, list[GatewayLogEntry]]]:
    groups: dict[str, list[GatewayLogEntry]] = {}
    order: list[str] = []
    for entry in entries:
        group_id = _follow_group_id([entry])
        if group_id not in groups:
            groups[group_id] = []
            order.append(group_id)
        groups[group_id].append(entry)
    return [(group_id, groups[group_id]) for group_id in order]


def _follow_lookup_session_id(
    args: argparse.Namespace,
    *,
    locked_session_id: str | None,
) -> str | None:
    if args.session_id:
        return args.session_id
    return None


def _next_locked_follow_session_id(
    args: argparse.Namespace,
    *,
    locked_session_id: str | None,
    current_session_id: str | None,
) -> str | None:
    return locked_session_id


def _should_print_session_separator(
    args: argparse.Namespace,
    *,
    printed_any: bool,
    session_changed: bool,
) -> bool:
    if not session_changed:
        return False
    if args.session_id:
        return False
    return True


def _events_signature(events: list[GatewayLogEntry]) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        (
            entry.timestamp,
            entry.fields.get("event"),
            entry.fields.get("run_id"),
            entry.fields.get("turn_id"),
            entry.fields.get("trace_id"),
            entry.fields.get("session_id"),
            entry.raw,
        )
        for entry in events
    )


def _follow_group_id(events: list[GatewayLogEntry]) -> str:
    for entry in events:
        run_id = _entry_run_id(entry)
        if run_id:
            return f"run:{run_id}"
    for entry in events:
        trace_id = _entry_trace_id(entry)
        if trace_id:
            return f"trace:{trace_id}"
    if events:
        event_name = _entry_event_name(events[-1])
        return f"event:{event_name}:{events[-1].timestamp}"
    return "empty"


def _follow_update_ready(events: list[GatewayLogEntry]) -> bool:
    has_run = any(_entry_run_id(entry) for entry in events)
    if not has_run:
        return bool(events)
    event_names = {_entry_event_name(entry) for entry in events}
    return bool(event_names & GATEWAY_TERMINAL_EVENTS)


def _format_gateway_payload(events: list[GatewayLogEntry]) -> str:
    lines = [_format_gateway_payload_header(events)]
    lines.extend(_format_entry(entry) for entry in events)
    return "\n".join(lines)


def _format_gateway_payload_header(events: list[GatewayLogEntry]) -> str:
    run_id = next((_entry_run_id(entry) for entry in events if _entry_run_id(entry)), None)
    trace_id = next((_entry_trace_id(entry) for entry in events if _entry_trace_id(entry)), None)
    status = _infer_gateway_status(events)
    if run_id:
        return f"gateway run {run_id} trace {trace_id or '-'} status={status} events={len(events)}"
    event_name = _entry_event_name(events[-1]) if events else "event"
    return f"gateway event {event_name} trace {trace_id or '-'} status={status} events={len(events)}"


def _infer_gateway_status(events: list[GatewayLogEntry]) -> str:
    for entry in reversed(events):
        event_name = _entry_event_name(entry)
        if event_name == "gateway.run.completed":
            status = entry.fields.get("status")
            return str(status) if status else "completed"
        if event_name == "gateway.run.cancelled":
            return "cancelled"
        if event_name == "gateway.run.errored":
            status = entry.fields.get("status")
            return str(status) if status else "errored"
        if event_name == "gateway.run.queue_rejected":
            return "queue_rejected"
        if event_name == "gateway.run.queue_expired":
            return "queue_expired"
    for entry in reversed(events):
        status = entry.fields.get("status")
        if isinstance(status, str) and status:
            return status
    if any(_entry_run_id(entry) for entry in events):
        return "running"
    return "observed" if events else "unknown"


def _gateway_session_id(events: list[GatewayLogEntry]) -> str | None:
    for entry in events:
        value = entry.fields.get("session_id")
        if isinstance(value, str) and value:
            return value
    return None


def _format_follow_session_separator(session_id: str | None) -> str:
    session_text = session_id or "(none)"
    return f"{FOLLOW_SESSION_SEPARATOR} SESSION {session_text} {FOLLOW_SESSION_SEPARATOR}"


def _entry_run_id(entry: GatewayLogEntry) -> str | None:
    value = entry.fields.get("run_id")
    return value if isinstance(value, str) and value else None


def _entry_trace_id(entry: GatewayLogEntry) -> str | None:
    value = entry.fields.get("trace_id")
    return value if isinstance(value, str) and value else None


def _entry_event_name(entry: GatewayLogEntry) -> str:
    value = entry.fields.get("event")
    return str(value) if value else "event"


def _print_entries(
    lines: Iterable[str],
    *,
    line_parser: Callable[[str], GatewayLogEntry | None],
) -> int:
    count = 0
    for line in lines:
        entry = line_parser(line)
        if entry is None:
            continue
        print(_format_entry(entry), flush=True)
        count += 1
    return count


def _parse_jsonl_event_line(raw_line: str) -> GatewayLogEntry | None:
    raw = raw_line.rstrip("\n")
    if not raw.strip():
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return GatewayLogEntry(raw=raw, timestamp="", fields={}, parsed=False)
    if not isinstance(payload, dict):
        return GatewayLogEntry(raw=raw, timestamp="", fields={}, parsed=False)
    event = payload.get("event")
    timestamp = payload.get("created_at")
    if not isinstance(event, str) or not event or not isinstance(timestamp, str):
        return GatewayLogEntry(raw=raw, timestamp="", fields={}, parsed=False)
    fields: dict[str, Any] = {
        "level": "INFO",
        "component": payload.get("component") or "gateway",
        "schema_version": payload.get("schema_version"),
        "created_at": timestamp,
        "event": event,
    }
    for key in ("run_id", "turn_id", "trace_id", "user_id", "session_id"):
        value = payload.get(key)
        if isinstance(value, str) and value and value != "-":
            fields[key] = value
    attributes = payload.get("attributes")
    if isinstance(attributes, dict):
        for key, value in attributes.items():
            if _is_scalar(value):
                fields[str(key)] = value
    return GatewayLogEntry(raw=raw, timestamp=timestamp, fields=fields, parsed=True)


def _parse_key_value_log_line(raw_line: str) -> GatewayLogEntry | None:
    raw = raw_line.rstrip("\n")
    if not raw.strip():
        return None
    try:
        tokens = shlex.split(raw)
    except ValueError:
        return GatewayLogEntry(raw=raw, timestamp="", fields={}, parsed=False)
    if not tokens:
        return None

    timestamp = ""
    if "=" not in tokens[0]:
        timestamp = tokens[0]
        tokens = tokens[1:]

    fields: dict[str, Any] = {}
    message_parts: list[str] = []
    for token in tokens:
        if "=" not in token:
            message_parts.append(token)
            continue
        key, value = token.split("=", 1)
        if not key:
            message_parts.append(token)
            continue
        fields[key] = value
    if message_parts:
        fields["message"] = " ".join(message_parts)

    if not timestamp or not fields.get("event"):
        return GatewayLogEntry(raw=raw, timestamp=timestamp, fields=fields, parsed=False)
    return GatewayLogEntry(raw=raw, timestamp=timestamp, fields=fields, parsed=True)


def _format_entry(entry: GatewayLogEntry) -> str:
    if not entry.parsed:
        return f"raw log line: {_shorten(entry.raw)}"
    level = entry.fields.get("level", "-")
    event = entry.fields.get("event", "-")
    label = EVENT_LABELS.get(event, _label_from_event(event))
    detail_parts = _detail_parts(entry)
    id_parts = _identifier_parts(entry)
    suffix = " ".join([label, *detail_parts, *id_parts]).strip()
    return f"{entry.timestamp} {level:<5} {event:<34} {suffix}".rstrip()


def _label_from_event(event: str) -> str:
    if not event:
        return "event"
    if event.startswith("gateway."):
        event = event[len("gateway.") :]
    return event.replace(".", " ").replace("_", " ")


def _detail_parts(entry: GatewayLogEntry) -> list[str]:
    event = entry.fields.get("event", "")
    ordered_keys = EVENT_DETAIL_KEYS.get(event, ())
    used = set(MACHINE_KEYS)
    used.update(IDENTIFIER_KEYS)
    parts: list[str] = []
    for key in ordered_keys:
        if key in entry.fields:
            parts.append(_format_field(key, entry.fields[key]))
            used.add(key)
    for key, value in entry.fields.items():
        if key in used:
            continue
        parts.append(_format_field(key, value))
    return parts


def _identifier_parts(entry: GatewayLogEntry) -> list[str]:
    parts: list[str] = []
    for key, label in IDENTIFIER_KEYS.items():
        value = entry.fields.get(key)
        if value and value != "-":
            parts.append(f"{label}={value}")
    return parts


def _format_field(key: str, value: str) -> str:
    label = PAYLOAD_KEY_LABELS.get(key, key)
    return f"{label}={value}"


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _shorten(value: str) -> str:
    if len(value) <= RAW_LINE_LIMIT:
        return value
    return value[: RAW_LINE_LIMIT - 1] + "…"


if __name__ == "__main__":
    raise SystemExit(main())
