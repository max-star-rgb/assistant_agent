#!/usr/bin/env python3
"""Inspect one redacted assistant run or trace from the local JSONL trace store."""

# ruff: noqa: E402 - repository src path must be installed before package imports.

from __future__ import annotations

import argparse
import json
import os
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

from assistant_agent.runtime.assistant_run_service import load_env_file
from assistant_agent.observability.langfuse_config import (
    langfuse_authorization_headers,
    langfuse_host_from_env,
)
from assistant_agent.observability.trace_store import TraceEvent, trace_debug_summary
from assistant_agent.observability.turn_evaluator import build_turn_diagnostic
from assistant_agent.observability.turn_summary import (
    ASSISTANT_TURN_SUMMARY_EVENT,
    ASSISTANT_TURN_SUMMARY_KEY,
    ASSISTANT_TURN_SUMMARY_SCHEMA_VERSION,
)


DEFAULT_TRACE_PATH = ".data/graph_trace.jsonl"
LATEST_IDENTIFIERS = {"last", "latest", "@last"}
TRACE_SECTION_ORDER = ("overview", "conversation", "decision", "timeline")
TRACE_SECTION_ALIASES = {
    "all": TRACE_SECTION_ORDER,
    "full": TRACE_SECTION_ORDER,
}
COMPAT_REACT_SECTION = "react"
FOLLOW_SESSION_SEPARATOR = "=" * 16
RUN_TERMINAL_EVENTS = frozenset({"run.completed", "run.failed", "run.cancelled"})
AGENT_SERVICE_TERMINAL_EVENTS = frozenset({"agent_service.turn.finished"})
AGENT_SERVICE_SESSION_PREFIX = "agent-service-"
REACT_DETAIL_EVENTS = {
    "llm.chat.finished",
    "react.decision",
    "action.validation.finished",
    "tool.started",
    "tool.finished",
    "tool.failed",
    "tool.observation",
    "loop_guard.triggered",
    "runtime.phase.changed",
    "tool.attempt.failed",
    "tool.retry.scheduled",
}
DETAIL_ATTRIBUTE_KEYS = (
    "decision_type",
    "tool_call_id",
    "iteration",
    "risk",
    "side_effect",
    "recovery_action",
    "retry_count",
    "execution_retry_count",
    "retry_exhausted",
    "attempt_count",
    "failed_attempt",
    "next_attempt",
    "max_attempts",
    "guard_code",
    "disposition",
    "from_phase",
    "to_phase",
    "reason",
    "source",
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
            "When set, query the running server for trace detail. "
            "The local JSONL file is still used to resolve latest/follow targets."
        ),
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Local env file used only for Langfuse fallback credentials. Defaults to .env.",
    )
    parser.add_argument(
        "--no-env-file",
        action="store_true",
        help="Do not load a local env file for Langfuse fallback credentials.",
    )
    parser.add_argument(
        "--trace-path",
        default=DEFAULT_TRACE_PATH,
        help=f"Local JSONL trace store path. Defaults to {DEFAULT_TRACE_PATH}.",
    )
    parser.add_argument(
        "--session-id",
        help="Filter local JSONL lookup to one session id, useful with last/latest/@last and --follow.",
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
            "Comma-separated output sections: overview,conversation,decision,timeline. "
            "react is accepted for compatibility and maps to decision. "
            "Use full/all for all sections. Defaults to overview."
        ),
    )
    parser.add_argument(
        "--react-detail",
        action="store_true",
        help="Compatibility flag. Adds the Decision Trace section.",
    )
    parser.add_argument(
        "--latency-stages",
        action="store_true",
        help="Expand per-stage rows inside Turn latency. Raw events usually own detailed stages.",
    )
    parser.add_argument("--json", dest="json_output", action="store_true", help="Print a JSON summary.")
    parser.add_argument(
        "--follow",
        action="store_true",
        help="Watch the local trace file and print updates until interrupted.",
    )
    parser.add_argument(
        "--follow-include-existing",
        action="store_true",
        help=(
            "With last/latest/@last --follow, print the currently matching run "
            "before waiting for newer trace updates."
        ),
    )
    parser.add_argument(
        "--follow-live-updates",
        action="store_true",
        help="Print non-terminal trace updates while a run is still in progress.",
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
    if args.follow and args.json_output:
        parser.error("--json cannot be combined with --follow")
    if args.follow and args.poll_interval <= 0:
        parser.error("--poll-interval must be greater than 0")
    if args.follow_limit is not None and args.follow_limit <= 0:
        parser.error("--follow-limit must be greater than 0")
    if args.follow_timeout is not None and args.follow_timeout < 0:
        parser.error("--follow-timeout must be greater than or equal to 0")
    sections = _parse_sections(parser, args.sections, include_conversation=args.include_conversation, react_detail=args.react_detail)
    include_conversation = "conversation" in sections
    if include_conversation:
        if not args.server:
            parser.error("conversation output requires --server")
        if not _is_loopback_server(args.server):
            parser.error("conversation output requires a loopback --server URL")
        if not args.no_env_file:
            load_env_file((REPO_ROOT / args.env_file).resolve())
    if args.follow:
        return _follow_local_trace(args, sections)
    if args.server:
        identifier = args.identifier
        local_events: list[TraceEvent] = []
        if _is_latest_identifier(identifier):
            local_events = _find_local_events(args.trace_path, identifier, session_id=args.session_id)
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
            conversation = _fetch_conversation(args.server, trace_id)
            if conversation is None:
                conversation = _conversation_unavailable_payload(trace_id)
            payload["conversation"] = conversation
        payload = _server_summary_payload(payload)
        if args.json_output:
            print(json.dumps(_json_ready(payload), ensure_ascii=False, indent=2))
            return 0
        print(_format_human(payload, show_errors=args.errors, sections=sections, show_latency_stages=args.latency_stages))
        return 0

    events = _find_local_events(args.trace_path, args.identifier, session_id=args.session_id)
    if not events:
        print(f"trace/run not found: {args.identifier}", file=sys.stderr)
        return 1

    payload = _summary_payload(events)
    if args.json_output:
        print(json.dumps(_json_ready(payload), ensure_ascii=False, indent=2))
        return 0

    print(_format_human(payload, show_errors=args.errors, sections=sections, show_latency_stages=args.latency_stages))
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
        requested = ["overview"]
        if include_conversation:
            requested.append("conversation")
        if react_detail:
            requested.append("decision")
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
            elif item == COMPAT_REACT_SECTION:
                requested.append("decision")
            else:
                parser.error(
                    "--sections must contain only overview,conversation,decision,timeline,react,full,all"
                )
    if not requested:
        parser.error("--sections must not be empty")
    return tuple(section for section in TRACE_SECTION_ORDER if section in set(requested))


def _is_latest_identifier(identifier: str) -> bool:
    return identifier.lower() in LATEST_IDENTIFIERS


def _find_local_events(trace_path: str | Path, identifier: str, *, session_id: str | None = None) -> list[TraceEvent]:
    events = _load_local_events(trace_path)
    if session_id:
        events = [event for event in events if event.session_id == session_id]
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


def _follow_local_trace(args: argparse.Namespace, sections: tuple[str, ...]) -> int:
    deadline = None if args.follow_timeout is None else monotonic() + args.follow_timeout
    suppressed_run_ids = _initial_suppressed_follow_run_ids(args)
    previous_session_id: str | None = None
    printed_any = False
    printed_updates = 0
    printed_run_ids: set[str] = set()
    printed_signatures: dict[str, tuple[tuple[Any, ...], ...]] = {}
    saw_matching_events = bool(_follow_event_groups(args, suppressed_run_ids=set()))
    locked_session_id: str | None = None
    while True:
        event_groups = _follow_event_groups(
            args,
            suppressed_run_ids=suppressed_run_ids,
            locked_session_id=locked_session_id,
        )
        if event_groups:
            saw_matching_events = True
        for events in event_groups:
            signature = _events_signature(events)
            run_id = events[0].run_id
            if not args.follow_live_updates and run_id in printed_run_ids:
                continue
            if args.follow_live_updates and printed_signatures.get(run_id) == signature:
                continue
            if not args.follow_live_updates and not _follow_update_ready(events):
                continue
            payload = _follow_payload(args, events, sections)
            current_session_id = _follow_session_id(payload, events)
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
                print(_format_follow_session_separator(payload, current_session_id))
            elif printed_any:
                print()
                print("--- trace update ---")
            print(
                _format_human(
                    payload,
                    show_errors=args.errors,
                    sections=sections,
                    show_latency_stages=args.latency_stages,
                ),
                flush=True,
            )
            printed_signatures[run_id] = signature
            printed_any = True
            if not args.follow_live_updates:
                printed_run_ids.add(run_id)
            printed_updates += 1
            if args.follow_limit is not None and printed_updates >= args.follow_limit:
                return 0
        if deadline is not None and monotonic() >= deadline:
            if printed_any:
                return 0
            if saw_matching_events:
                return 0
            print(f"trace/run not found: {args.identifier}", file=sys.stderr)
            return 1
        sleep(args.poll_interval)


def _initial_suppressed_follow_run_ids(args: argparse.Namespace) -> set[str]:
    if not _is_latest_identifier(args.identifier):
        return set()
    events = _load_local_events(args.trace_path)
    lookup_session_id = _follow_lookup_session_id(args, locked_session_id=None)
    if lookup_session_id:
        events = [event for event in events if event.session_id == lookup_session_id]
    groups = _group_trace_events_by_run(events)
    if not args.follow_include_existing:
        return {run_id for run_id, _ in groups}
    latest_events = _latest_run_events(events)
    latest_run_id = latest_events[0].run_id if latest_events else None
    return {run_id for run_id, _ in groups if run_id != latest_run_id}


def _follow_event_groups(
    args: argparse.Namespace,
    *,
    suppressed_run_ids: set[str],
    locked_session_id: str | None = None,
) -> list[list[TraceEvent]]:
    lookup_session_id = _follow_lookup_session_id(args, locked_session_id=locked_session_id)
    if not _is_latest_identifier(args.identifier):
        events = _find_local_events(args.trace_path, args.identifier, session_id=lookup_session_id)
        if not events or events[0].run_id in suppressed_run_ids:
            return []
        return [events]
    events = _load_local_events(args.trace_path)
    if lookup_session_id:
        events = [event for event in events if event.session_id == lookup_session_id]
    return [
        group
        for run_id, group in _group_trace_events_by_run(events)
        if run_id not in suppressed_run_ids
    ]


def _group_trace_events_by_run(events: list[TraceEvent]) -> list[tuple[str, list[TraceEvent]]]:
    groups: dict[str, list[TraceEvent]] = {}
    order: list[str] = []
    for event in events:
        run_id = event.run_id
        if run_id not in groups:
            groups[run_id] = []
            order.append(run_id)
        groups[run_id].append(event)
    return [(run_id, groups[run_id]) for run_id in order]


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


def _events_signature(events: list[TraceEvent]) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        (
            event.trace_id,
            event.run_id,
            event.session_id,
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


def _follow_update_ready(events: list[TraceEvent]) -> bool:
    event_names = {_trace_event_name(event) for event in events}
    if ASSISTANT_TURN_SUMMARY_EVENT in event_names:
        return True
    if event_names & AGENT_SERVICE_TERMINAL_EVENTS:
        return True
    if _is_agent_service_trace(events):
        return False
    return bool(event_names & RUN_TERMINAL_EVENTS)


def _trace_event_name(event: TraceEvent) -> str:
    return str(event.canonical_event or event.event_type or event.node_name or "event")


def _is_agent_service_trace(events: list[TraceEvent]) -> bool:
    return any(
        (event.session_id or "").startswith(AGENT_SERVICE_SESSION_PREFIX)
        or event.node_name == "agent_service"
        or _trace_event_name(event).startswith("agent_service.")
        for event in events
    )


def _follow_payload(args: argparse.Namespace, events: list[TraceEvent], sections: tuple[str, ...]) -> dict[str, Any]:
    if not args.server:
        return _summary_payload(events)

    trace_id = events[0].trace_id
    payload = _fetch_server_trace(args.server, trace_id)
    if payload is None:
        payload = _summary_payload(events)
    if "conversation" in sections:
        conversation_trace_id = payload.get("trace_id")
        if not isinstance(conversation_trace_id, str) or not conversation_trace_id:
            conversation_trace_id = trace_id
        conversation = _fetch_conversation(args.server, conversation_trace_id)
        if conversation is None:
            conversation = _conversation_unavailable_payload(conversation_trace_id)
        payload["conversation"] = conversation
    return _server_summary_payload(payload)


def _conversation_unavailable_payload(trace_id: str) -> dict[str, Any]:
    return {
        "schema_version": "trace_conversation_unavailable_v1",
        "trace_id": trace_id,
        "unavailable": True,
        "reason": "conversation is unavailable from both the server and Langfuse",
    }


def _fetch_conversation(server: str, trace_id: str) -> dict[str, Any] | None:
    """Prefer live process memory, then use persisted Langfuse trace content."""

    conversation = _get_server_json(
        server,
        f"/traces/{quote(trace_id, safe='')}/conversation",
    )
    if conversation is not None:
        return conversation
    trace = _get_langfuse_trace(trace_id)
    if trace is None:
        return None
    return _conversation_from_langfuse_trace(trace_id, trace)


def _get_langfuse_trace(
    trace_id: str,
    *,
    env: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Read one persisted trace through Langfuse's authenticated Public API."""

    values = os.environ if env is None else env
    host = langfuse_host_from_env(values).rstrip("/")
    authorization_headers = langfuse_authorization_headers(values)
    if not authorization_headers:
        return None
    url = f"{host}/api/public/traces/{quote(trace_id, safe='')}"
    request = Request(
        url,
        headers={"Accept": "application/json", **authorization_headers},
    )
    try:
        with urlopen(request, timeout=10) as response:
            body = response.read().decode("utf-8")
    except (HTTPError, URLError):
        return None
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _conversation_from_langfuse_trace(
    trace_id: str,
    trace: dict[str, Any],
) -> dict[str, Any]:
    observations = trace.get("observations")
    if not isinstance(observations, list):
        observations = []
    llm_observations = [
        item
        for item in observations
        if isinstance(item, dict) and str(item.get("name", "")).startswith("llm.chat")
    ]
    user_text = _role_content(trace.get("input"), role="user")
    assistant_text = _role_content(trace.get("output"), role="assistant")
    return {
        "schema_version": "trace_conversation_view_v1",
        "trace_id": trace_id,
        "source": "langfuse_public_api",
        "user": _conversation_text(user_text),
        "assistant": _conversation_text(assistant_text),
        "llm_inputs": [
            {"request": _json_object(item.get("input"))}
            for item in llm_observations
        ],
        "llm_outputs": [
            _json_object(item.get("output"))
            for item in llm_observations
        ],
    }


def _conversation_text(text: str) -> dict[str, Any]:
    return {"text": text, "chars": len(text), "truncated": False}


def _role_content(value: Any, *, role: str) -> str:
    payload = _json_object(value)
    if payload.get("role") == role and isinstance(payload.get("content"), str):
        return payload["content"]
    content = payload.get("content")
    return content if isinstance(content, str) else ""


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"content": value}
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    return {}


def _follow_session_id(payload: dict[str, Any], events: list[TraceEvent]) -> str | None:
    session_id = payload.get("session_id")
    if isinstance(session_id, str) and session_id:
        return session_id
    return events[0].session_id


def _format_follow_session_separator(payload: dict[str, Any], session_id: str | None) -> str:
    session_text = session_id or "(none)"
    return f"{FOLLOW_SESSION_SEPARATOR} SESSION {session_text} {FOLLOW_SESSION_SEPARATOR}"


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
    turn_summary = _payload_turn_summary(summary) or _latest_turn_summary(events)
    if isinstance(turn_summary, dict):
        summary["turn_summary"] = turn_summary
        _apply_turn_summary_payload(summary, turn_summary)
    if not isinstance(summary.get("turn_latency"), dict):
        turn_latency = _latest_turn_latency_summary(events)
        if turn_latency is not None:
            summary["turn_latency"] = turn_latency
    error_count = summary.get("error_count")
    if not isinstance(error_count, int):
        error_count = len(_error_events(events))
        summary["error_count"] = error_count
    if not isinstance(summary.get("event_count"), int):
        summary["event_count"] = len(events)
    if not isinstance(summary.get("status"), str):
        summary["status"] = _infer_status(events, error_count)
    if not isinstance(summary.get("duration_ms"), int):
        duration_ms = _duration_ms_from_event_dicts(events)
        if duration_ms is not None:
            summary["duration_ms"] = duration_ms
    return summary


def _summary_payload(events: list[TraceEvent]) -> dict[str, Any]:
    summary = trace_debug_summary(events)
    _add_timing_fields(summary["events"])
    turn_summary = _latest_turn_summary(summary["events"])
    if isinstance(turn_summary, dict):
        summary["turn_summary"] = turn_summary
        _apply_turn_summary_payload(summary, turn_summary)
    turn_latency = _latest_turn_latency_summary(summary["events"])
    if turn_latency is not None:
        summary["turn_latency"] = turn_latency
    if not isinstance(summary.get("status"), str):
        summary["status"] = _infer_status(summary["events"], summary["error_count"])
    summary["event_count"] = len(events)
    summary["duration_ms"] = _duration_ms(events)
    return summary


def _payload_turn_summary(payload: dict[str, Any]) -> dict[str, Any] | None:
    value = payload.get("turn_summary")
    return dict(value) if _is_turn_summary(value) else None


def _latest_turn_summary(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in reversed(events):
        output_summary = event.get("output_summary")
        if not isinstance(output_summary, dict):
            continue
        value = output_summary.get(ASSISTANT_TURN_SUMMARY_KEY)
        if _is_turn_summary(value):
            return dict(value)
    return None


def _latest_turn_latency_summary(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in reversed(events):
        output_summary = event.get("output_summary")
        if not isinstance(output_summary, dict):
            continue
        value = output_summary.get("turn_latency")
        if (
            isinstance(value, dict)
            and value.get("schema_version")
            in {"agent_service_turn_latency_v1", "agent_service_turn_latency_v2"}
        ):
            return dict(value)
    return None


def _is_turn_summary(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("schema_version") == ASSISTANT_TURN_SUMMARY_SCHEMA_VERSION
    )


def _apply_turn_summary_payload(payload: dict[str, Any], turn_summary: dict[str, Any]) -> None:
    for source_key, target_key in (
        ("trace_id", "trace_id"),
        ("assistant_run_id", "run_id"),
        ("user_id", "user_id"),
        ("session_id", "session_id"),
    ):
        value = turn_summary.get(source_key)
        if isinstance(value, str) and value:
            payload[target_key] = value
    terminal_status = turn_summary.get("terminal_status")
    if isinstance(terminal_status, str) and terminal_status:
        payload["status"] = terminal_status
    for key in ("entry_status", "runtime_status"):
        value = turn_summary.get(key)
        if isinstance(value, str) and value:
            payload[key] = value
    error_count = turn_summary.get("error_count")
    if isinstance(error_count, int) and not isinstance(error_count, bool):
        payload["error_count"] = error_count


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


def _duration_ms_from_event_dicts(events: list[dict[str, Any]]) -> int | None:
    if not events:
        return None
    started_at = _event_created_at(events[0])
    finished_at = _event_created_at(events[-1])
    if started_at is None or finished_at is None:
        return None
    return max(0, round((finished_at - started_at).total_seconds() * 1000))


def _format_human(
    payload: dict[str, Any],
    *,
    show_errors: bool,
    sections: tuple[str, ...] = ("timeline",),
    show_latency_stages: bool = False,
) -> str:
    lines = [_format_header(payload)]
    events = payload.get("events", [])
    conversation = payload.get("conversation")
    if "overview" in sections:
        lines.extend(("", *_format_turn_overview(payload)))
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
    if "decision" in sections:
        lines.extend(("", *_format_decision_trace(events)))
    if "timeline" in sections:
        turn_latency = payload.get("turn_latency")
        if isinstance(turn_latency, dict):
            lines.extend(("", *_format_turn_latency(turn_latency, show_stages=show_latency_stages)))
        lines.extend(("", "Raw events"))
        for index, event in enumerate(events, start=1):
            lines.append(_format_event_line(index, event))
    return "\n".join(lines)


def _format_turn_overview(payload: dict[str, Any]) -> list[str]:
    diagnostic = _turn_diagnostic(payload)
    lines = [
        "Turn Overview",
        "  "
        f"execution={diagnostic['execution_status']} "
        f"delivery={diagnostic['delivery_status']} "
        f"task_outcome={diagnostic['task_outcome']} "
        f"ux_outcome={diagnostic['text_ux_status']}",
        "",
        "Performance",
        f"  Total latency    {_milliseconds(diagnostic.get('total_latency_ms'))}",
        "  "
        f"First response   first_text={_milliseconds_or_unknown(diagnostic.get('first_text_latency_ms'))}",
    ]
    for tool in diagnostic["tool_summary"]:
        lines.append(
            "  "
            f"{tool['tool_name']:<16} x{tool['count']} "
            f"{_milliseconds(tool.get('total_latency_ms'))}"
        )
    llm = diagnostic["llm_summary"]
    if llm["count"]:
        lines.append(
            "  "
            f"LLM chat x{llm['count']:<7} "
            f"{_milliseconds(llm.get('wall_latency_ms'))}"
        )
        if isinstance(llm.get("provider_latency_ms"), int):
            lines.append(
                "  "
                f"LLM wall         {_milliseconds(llm.get('max_wall_latency_ms'))} "
                f"provider={_milliseconds(llm.get('max_provider_latency_ms'))} "
                f"overhead={_milliseconds(llm.get('max_overhead_ms'))}"
            )
    context_peak = diagnostic.get("context_peak_ratio")
    if isinstance(context_peak, float):
        lines.append(f"  Context peak     {_percent(context_peak)}")

    lines.extend(("", "Decision path"))
    path = diagnostic["decision_path"]
    if path:
        lines.extend(f"  {item}" for item in path)
    else:
        lines.append("  unknown")

    lines.extend(("", "Main issues"))
    flags = diagnostic["diagnostic_flags"]
    if flags:
        lines.extend(f"  {item}" for item in flags)
    else:
        lines.append("  none")

    lines.extend(("", "Suggested actions"))
    suggestions = diagnostic["suggested_actions"]
    if suggestions:
        for index, item in enumerate(suggestions, start=1):
            lines.append(f"  {index}. {item}")
    else:
        lines.append("  none")
    return lines


def _turn_diagnostic(payload: dict[str, Any]) -> dict[str, Any]:
    events = payload.get("events")
    event_items = [item for item in events if isinstance(item, dict)] if isinstance(events, list) else []
    return build_turn_diagnostic(event_items, payload=payload).model_dump(mode="python")


def _execution_status(payload: dict[str, Any]) -> str:
    status = str(payload.get("status") or "unknown")
    error_count = payload.get("error_count")
    if status == "completed" and error_count == 0:
        return "success"
    if status in {"failed", "cancelled"}:
        return status
    if isinstance(error_count, int) and error_count > 0:
        return "failed"
    return "unknown"


def _delivery_status(turn_latency: Any, turn_summary: Any) -> str:
    if isinstance(turn_latency, dict):
        status = str(turn_latency.get("status") or "").lower()
        ack = str(turn_latency.get("ack_status") or "").lower()
        if status in {"sent", "acked", "success", "completed"} or ack == "acked":
            return "success"
        if status in {"failed", "disconnected_before_send"}:
            return "failed"
    if isinstance(turn_summary, dict) and turn_summary.get("response_present") is True:
        return "response_ready"
    return "unknown"


def _task_outcome(payload: dict[str, Any], turn_summary: Any) -> str:
    for source in (payload, turn_summary):
        if not isinstance(source, dict):
            continue
        value = source.get("task_outcome")
        if isinstance(value, str) and value:
            return value
    return "unknown"


def _text_ux_status(*, first_text_latency_ms: int | None) -> str:
    if first_text_latency_ms is None:
        return "unknown"
    return "measured"


def _llm_latency_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    count = 0
    wall_total = 0
    provider_total = 0
    max_wall = 0
    max_provider = 0
    for event in events:
        if _event_name(event) != "llm.chat.finished":
            continue
        count += 1
        attributes = event.get("attributes")
        wall = _int_from_mapping(attributes, "wall_latency_ms") if isinstance(attributes, dict) else None
        if wall is None:
            wall = event.get("latency_ms") if isinstance(event.get("latency_ms"), int) else 0
        provider = _int_from_mapping(attributes, "provider_latency_ms") if isinstance(attributes, dict) else None
        wall_total += wall
        max_wall = max(max_wall, wall)
        if provider is not None:
            provider_total += provider
            max_provider = max(max_provider, provider)
    max_overhead = max(0, max_wall - max_provider) if max_provider else None
    return {
        "count": count,
        "wall_latency_ms": wall_total if count else None,
        "provider_latency_ms": provider_total if provider_total else None,
        "overhead_ms": max(0, wall_total - provider_total) if provider_total else None,
        "max_wall_latency_ms": max_wall if count else None,
        "max_provider_latency_ms": max_provider if max_provider else None,
        "max_overhead_ms": max_overhead,
    }


def _tool_latency_summary(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summaries: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for event in events:
        if _event_name(event) not in {"tool.finished", "tool.failed"}:
            continue
        tool_name = event.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name:
            tool_name = "unknown_tool"
        if tool_name not in summaries:
            summaries[tool_name] = {"tool_name": tool_name, "count": 0, "total_latency_ms": 0}
            order.append(tool_name)
        summary = summaries[tool_name]
        summary["count"] += 1
        latency_ms = event.get("latency_ms")
        if isinstance(latency_ms, int):
            summary["total_latency_ms"] += latency_ms
    return [summaries[name] for name in order]


def _context_peak_ratio(events: list[dict[str, Any]]) -> float | None:
    peak: float | None = None
    for event in events:
        candidates: list[Any] = []
        attributes = event.get("attributes")
        if isinstance(attributes, dict):
            candidates.append(attributes.get("context_usage_ratio"))
        output_summary = event.get("output_summary")
        if isinstance(output_summary, dict):
            context = output_summary.get("context")
            if isinstance(context, dict):
                budget = context.get("budget")
                if isinstance(budget, dict):
                    candidates.append(budget.get("context_usage_ratio"))
        for value in candidates:
            ratio = _ratio_value(value)
            if ratio is not None:
                peak = ratio if peak is None else max(peak, ratio)
    return peak


def _decision_path(
    events: list[dict[str, Any]],
    *,
    llm_summary: dict[str, Any],
    tool_summary: list[dict[str, Any]],
) -> list[str]:
    path: list[str] = []
    if llm_summary.get("count"):
        path.append(f"LLM chat x{llm_summary['count']}")
    decisions = [
        event
        for event in events
        if _event_name(event) == "react.decision"
    ]
    for event in decisions[:3]:
        output_summary = event.get("output_summary")
        decision = None
        if isinstance(output_summary, dict):
            decision = output_summary.get("decision_type")
        decision = decision or event.get("status") or "unknown"
        tool = event.get("tool_name")
        suffix = f" {tool}" if isinstance(tool, str) and tool else ""
        path.append(f"Decision {decision}{suffix}")
    for tool in tool_summary:
        path.append(f"Tool {tool['tool_name']} x{tool['count']}")
    return path


def _diagnostic_flags(
    *,
    llm_summary: dict[str, Any],
    context_peak: float | None,
    turn_latency: Any,
    first_text_latency_ms: int | None,
    tool_summary: list[dict[str, Any]],
) -> list[str]:
    flags: list[str] = []
    overhead = llm_summary.get("max_overhead_ms")
    provider = llm_summary.get("max_provider_latency_ms")
    if isinstance(overhead, int) and overhead >= 1000 and isinstance(provider, int):
        flags.append(f"P0 LLM overhead {overhead}ms exceeds provider latency")
    if isinstance(context_peak, float) and context_peak >= 0.8:
        flags.append(f"P1 Context peak {_percent(context_peak)}")
    if isinstance(turn_latency, dict):
        delivery_status = str(turn_latency.get("status") or "").lower()
        if delivery_status in {"sent", "acked", "success", "completed"} and first_text_latency_ms is None:
            flags.append("P1 first text latency is missing")
    if any(
        tool["tool_name"] in {"web_search", "web_fetch"}
        and tool["count"] >= 3
        and tool.get("total_latency_ms", 0) >= 3000
        for tool in tool_summary
    ):
        flags.append("P1 repeated read-only tool calls may be serial or over-broad")
    return flags


def _suggested_actions(flags: list[str]) -> list[str]:
    suggestions: list[str] = []
    if any("LLM overhead" in flag for flag in flags):
        suggestions.append("Break down LLM queue, request build, TTFT, stream consume, parse, and finalize timing.")
    if any("Context peak" in flag for flag in flags):
        suggestions.append("Inspect system prompt, tool schemas, and tool observations as primary context contributors.")
    if any("first text latency" in flag for flag in flags):
        suggestions.append("Record first text response latency for text turns.")
    if any("read-only tool calls" in flag for flag in flags):
        suggestions.append("Review whether same-batch read-only tool calls can run concurrently or be narrowed.")
    return suggestions


def _first_int(source: Any, keys: tuple[str, ...]) -> int | None:
    if not isinstance(source, dict):
        return None
    for key in keys:
        value = source.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _int_from_mapping(source: dict[str, Any], key: str) -> int | None:
    value = source.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _ratio_value(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        ratio = float(value)
        if ratio > 1:
            ratio = ratio / 100
        return max(0.0, ratio)
    return None


def _format_decision_trace(events: list[dict[str, Any]]) -> list[str]:
    lines = ["Decision Trace"]
    iterations = _decision_iterations(events)
    if not iterations:
        lines.append("  (none)")
        return lines
    for iteration in sorted(iterations):
        events_for_iteration = iterations[iteration]
        lines.append(f"Iteration {iteration}")
        decision = next(
            (event for event in events_for_iteration if _event_name(event) == "react.decision"),
            None,
        )
        if decision is not None:
            lines.extend(_format_iteration_decision(decision))
        tool_lines = [
            _format_iteration_tool_event(event)
            for event in events_for_iteration
            if _event_name(event) in {"tool.finished", "tool.failed"}
        ]
        if tool_lines:
            lines.extend(tool_lines)
    return lines


def _decision_iterations(events: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    iterations: dict[int, list[dict[str, Any]]] = {}
    for event in events:
        if _event_name(event) not in REACT_DETAIL_EVENTS:
            continue
        attributes = event.get("attributes")
        iteration = 0
        if isinstance(attributes, dict):
            value = attributes.get("iteration")
            if isinstance(value, int) and not isinstance(value, bool):
                iteration = value
        iterations.setdefault(iteration, []).append(event)
    return iterations


def _format_iteration_decision(event: dict[str, Any]) -> list[str]:
    output_summary = event.get("output_summary")
    decision = event.get("status") or "unknown"
    reason = None
    if isinstance(output_summary, dict):
        decision = output_summary.get("decision_type") or decision
        reason = output_summary.get("reason")
    tool = event.get("tool_name")
    tool_suffix = f" {tool}" if isinstance(tool, str) and tool else ""
    lines = [f"  Decision  {_plain_value(decision)}{tool_suffix}"]
    if isinstance(reason, str) and reason:
        lines.append(f"  Reason    {_compact_value(reason)}")
    return lines


def _format_iteration_tool_event(event: dict[str, Any]) -> str:
    tool_name = _plain_value(event.get("tool_name"))
    latency = _milliseconds(event.get("latency_ms"))
    status = _plain_value(event.get("status"))
    result_text = _tool_result_text(event.get("output_summary"))
    suffix = f" {result_text}" if result_text else ""
    return f"  Tool      {tool_name} {latency} {status}{suffix}"


def _tool_result_text(output_summary: Any) -> str:
    if not isinstance(output_summary, dict):
        return ""
    result_count = output_summary.get("result_count")
    if isinstance(result_count, int):
        label = "result" if result_count == 1 else "results"
        return f"{result_count} {label}"
    item_count = output_summary.get("item_count")
    if isinstance(item_count, int):
        label = "item" if item_count == 1 else "items"
        return f"{item_count} {label}"
    return ""


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


def _format_turn_latency(summary: dict[str, Any], *, show_stages: bool = False) -> list[str]:
    status = _plain_value(summary.get("status"))
    delivery = _plain_value(summary.get("delivery_id"))
    session_turn = _plain_value(summary.get("session_turn"))
    total = _milliseconds(summary.get("total_ms"))
    lines = [
        "Turn latency",
        f"  status={status} runtime={_plain_value(summary.get('runtime_status'))} "
        f"delivery={delivery} session_turn={session_turn} total={total}",
        "  "
        f"trace={_plain_value(summary.get('trace_id'))} "
        f"gateway_run={_plain_value(summary.get('gateway_run_id'))} "
        f"assistant_run={_plain_value(summary.get('assistant_run_id'))}",
    ]
    failure_code = summary.get("failure_code")
    if failure_code:
        lines.append(
            f"  failure={_plain_value(failure_code)} "
            f"source={_plain_value(summary.get('failure_source'))}"
        )
    active_stage = summary.get("active_stage")
    if active_stage:
        lines.append(
            f"  active_stage={_plain_value(active_stage)} "
            f"open_spans={_plain_value(summary.get('open_span_count'))}"
        )
    bottleneck = summary.get("bottleneck")
    if bottleneck:
        share = summary.get("bottleneck_share_pct")
        share_text = f" ({share}%)" if isinstance(share, (int, float)) else ""
        lines.append(
            f"  bottleneck={_plain_value(bottleneck)} "
            f"{_milliseconds(summary.get('bottleneck_ms'))}{share_text}"
        )

    if show_stages:
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
        ("semantic_publish_latency_ms", "semantic_publish_latency", True),
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
    if conversation.get("unavailable") is True:
        return [
            "Conversation",
            f"  (unavailable: {_plain_value(conversation.get('reason'))})",
        ]
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


def _milliseconds_or_unknown(value: Any) -> str:
    return f"{value}ms" if isinstance(value, int) else "unknown"


def _percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def _format_header(payload: dict[str, Any]) -> str:
    duration = payload.get("duration_ms")
    duration_part = f" duration={duration}ms" if isinstance(duration, int) else ""
    turn_summary = payload.get("turn_summary")
    client_part = ""
    if isinstance(turn_summary, dict):
        client_part = f" client={_plain_value(turn_summary.get('client_type'))}"
    return (
        f"run {payload.get('run_id')} trace {payload.get('trace_id')} "
        f"status={payload.get('status')} events={payload.get('event_count', 0)} "
        f"errors={payload.get('error_count', 0)}{client_part}{duration_part}"
    )


def _format_event_line(index: int, event: dict[str, Any]) -> str:
    name = _event_name(event)
    details = _event_details(event)
    suffix = f" {' '.join(details)}" if details else ""
    return f"{index:02d}  {_event_clock(event):<18} {name:<34}{suffix}"


def _format_error_line(index: int, event: dict[str, Any]) -> str:
    name = _event_name(event)
    details = _event_details(event)
    message = event.get("error_message")
    if isinstance(message, str) and message:
        details.append(f"message={_compact_value(message)}")
    suffix = f" {' '.join(details)}" if details else ""
    return f"{index:02d}  {_event_clock(event):<18} {name:<34}{suffix}"


def _event_name(event: dict[str, Any]) -> str:
    name = event.get("canonical_event") or event.get("event_type") or event.get("node_name")
    return str(name or "event")


def _event_clock(event: dict[str, Any]) -> str:
    elapsed_ms = event.get("elapsed_ms")
    if not isinstance(elapsed_ms, int):
        return "t+?"
    gap_ms = event.get("gap_ms")
    if isinstance(gap_ms, int) and gap_ms > 0:
        return f"t+{elapsed_ms}ms dt+{gap_ms}ms"
    return f"t+{elapsed_ms}ms"


def _event_details(event: dict[str, Any]) -> list[str]:
    details: list[str] = []
    name = _event_name(event)
    latency_ms = event.get("latency_ms")
    if isinstance(latency_ms, int):
        details.append(f"latency={latency_ms}ms")
    _append_named(details, "status", event.get("status"))
    _append_named(details, "tool", event.get("tool_name"))
    _append_named(details, "provider", event.get("provider"))
    _append_named(details, "model", event.get("model"))
    _append_named(details, "error", event.get("error_code"))
    if name in REACT_DETAIL_EVENTS:
        _append_react_timeline_details(details, event)
    if name == "context.build.finished":
        _append_context_tool_exposure(details, event.get("output_summary"))
    if name in REACT_DETAIL_EVENTS:
        _append_selected(details, event.get("attributes"), _react_remaining_attribute_keys(name))
    else:
        _append_selected(details, event.get("output_summary"), DETAIL_OUTPUT_KEYS)
        _append_selected(details, event.get("attributes"), DETAIL_ATTRIBUTE_KEYS)
    return details


def _append_react_timeline_details(details: list[str], event: dict[str, Any]) -> None:
    name = _event_name(event)
    attributes = event.get("attributes")
    output_summary = event.get("output_summary")
    if isinstance(attributes, dict):
        _append_named(details, "iteration", attributes.get("iteration"))
        _append_named(details, "batch", _batch_value(attributes))
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
    elif name.startswith("tool."):
        if isinstance(attributes, dict):
            _append_named(details, "tool_call_id", attributes.get("tool_call_id"))
            _append_named(details, "recovery", attributes.get("recovery_action"))
        if isinstance(output_summary, dict):
            _append_named(details, "output_ref", output_summary.get("output_ref"))
            _append_named(details, "artifact", output_summary.get("artifact_ref") or output_summary.get("artifact_id"))
            _append_named(details, "results", output_summary.get("result_count") or output_summary.get("item_count"))


def _react_remaining_attribute_keys(name: str) -> tuple[str, ...]:
    skipped = {
        "iteration",
        "decision_type",
        "risk",
        "side_effect",
        "tool_call_id",
        "recovery_action",
    }
    if not name.startswith("tool."):
        skipped.update({"retry_count"})
    return tuple(key for key in DETAIL_ATTRIBUTE_KEYS if key not in skipped)


def _append_context_tool_exposure(details: list[str], output_summary: Any) -> None:
    if not isinstance(output_summary, dict):
        return
    context = output_summary.get("context")
    if not isinstance(context, dict):
        return
    tool_catalog = context.get("tool_catalog")
    if isinstance(tool_catalog, dict):
        selected = tool_catalog.get("selected_tool_names")
        if isinstance(selected, list) and selected:
            details.append(f"selected_tools={_compact_value(selected)}")
    run_tool_catalog = context.get("run_tool_catalog")
    if isinstance(run_tool_catalog, dict):
        excluded = run_tool_catalog.get("excluded_reasons")
        if isinstance(excluded, dict) and excluded:
            details.append(f"excluded_tools={_compact_value(excluded)}")


def _append_named(details: list[str], name: str, value: Any) -> None:
    if value is None or value == "":
        return
    item = f"{name}={_compact_value(value)}"
    if item not in details:
        details.append(item)


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
