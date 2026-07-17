#!/usr/bin/env python3
"""Render prompt-safe Gateway lifecycle logs as a developer timeline."""

from __future__ import annotations

import argparse
import json
import shlex
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from time import monotonic, sleep


DEFAULT_GATEWAY_EVENT_PATH = ".data/gateway_events.jsonl"
DEFAULT_TAIL_LINES = 50
DEFAULT_POLL_INTERVAL = 1.0
RAW_LINE_LIMIT = 240

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
        "--tail",
        type=int,
        default=DEFAULT_TAIL_LINES,
        help=f"Print the last N lines before exiting or following. Defaults to {DEFAULT_TAIL_LINES}.",
    )
    parser.add_argument(
        "--follow",
        action="store_true",
        help="Continue watching the Gateway log after printing the requested tail.",
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
    if args.tail < 0:
        parser.error("--tail must be greater than or equal to 0")
    if args.poll_interval <= 0:
        parser.error("--poll-interval must be greater than 0")
    if args.follow_limit is not None and args.follow_limit <= 0:
        parser.error("--follow-limit must be greater than 0")
    if args.follow_timeout is not None and args.follow_timeout < 0:
        parser.error("--follow-timeout must be greater than or equal to 0")

    source_path, source_format, line_parser = _source(args)
    print(
        _format_header(source_path, source_format=source_format, follow=args.follow, tail=args.tail),
        flush=True,
    )

    printed_entries = _print_tail(source_path, args.tail, line_parser=line_parser)
    if args.follow_limit is not None and printed_entries >= args.follow_limit:
        return 0
    if not args.follow:
        if printed_entries == 0:
            print("  (no gateway log entries)", flush=True)
        return 0
    return _follow_log(
        source_path,
        initial_offset=_file_size(source_path),
        line_parser=line_parser,
        poll_interval=args.poll_interval,
        already_printed=printed_entries,
        follow_limit=args.follow_limit,
        follow_timeout=args.follow_timeout,
    )


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


def _follow_log(
    source_path: Path,
    *,
    initial_offset: int,
    line_parser: Callable[[str], GatewayLogEntry | None],
    poll_interval: float,
    already_printed: int,
    follow_limit: int | None,
    follow_timeout: float | None,
) -> int:
    offset = initial_offset
    printed_entries = already_printed
    deadline = None if follow_timeout is None else monotonic() + follow_timeout
    while True:
        new_lines, offset = _read_new_lines(source_path, offset)
        if new_lines:
            printed_entries += _print_entries(new_lines, line_parser=line_parser)
            if follow_limit is not None and printed_entries >= follow_limit:
                return 0
        if deadline is not None and monotonic() >= deadline:
            return 0
        sleep(poll_interval)


def _read_new_lines(source_path: Path, offset: int) -> tuple[list[str], int]:
    if not source_path.exists():
        return [], 0
    size = _file_size(source_path)
    if size < offset:
        offset = 0
    with source_path.open("r", encoding="utf-8") as file:
        file.seek(offset)
        lines = [line for line in file if line.strip()]
        return lines, file.tell()


def _file_size(source_path: Path) -> int:
    try:
        return source_path.stat().st_size
    except OSError:
        return 0


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
