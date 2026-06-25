#!/usr/bin/env python3
"""Remote CLI client for the assistant backend.

This script is a thin, pure client of a running FastAPI backend (start it with
`scripts/run_server.py`). It streams the assistant run over the same WebSocket
endpoint the Web Console uses (`/ws/agent/{session_id}`), so the CLI and the Web
UI are both clients of one backend service/API. The server process owns
provider/env configuration.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

import httpx
from websockets.sync.client import connect as ws_connect


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from multimodal_agent.schemas.events import AgentEvent
from multimodal_agent.services.demo_examples import get_demo_examples


DEFAULT_SERVER = "http://127.0.0.1:8000"


class RemoteServerError(RuntimeError):
    """Raised when the backend server cannot be reached or returns an invalid stream."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the ReAct assistant loop through a running FastAPI server.",
    )
    parser.add_argument("query", nargs="*", help="Optional query. Omit for interactive mode.")
    parser.add_argument(
        "--server",
        default=DEFAULT_SERVER,
        help="Backend server base URL, e.g. http://127.0.0.1:8000.",
    )
    parser.add_argument("--image-ref", action="append", default=[], help="Optional image id/ref for the request.")
    parser.add_argument("--video-ref", action="append", default=[], help="Optional video id/ref for the request.")
    parser.add_argument("--user-id", default="demo_user", help="User id for this demo run.")
    parser.add_argument("--session-id", default="demo_session", help="Session id for this demo run.")
    parser.add_argument("--json", action="store_true", help="Print a machine-readable JSON payload.")
    parser.add_argument("--no-live-events", action="store_true", help="Do not print live runtime events.")
    parser.add_argument("--show-trace", action="store_true", help="Print the full Decision Trace after the run.")
    parser.add_argument("--debug-events", action="store_true", help="Print raw runtime events instead of the compact timeline.")
    parser.add_argument("--save-log", help="Write a replayable run log to a file or directory.")
    parser.add_argument("--replay-log", help="Replay the request stored in a previous --save-log output.")
    return parser


def print_header() -> None:
    print()
    print("=" * 72)
    print("ReAct Assistant Loop - Runtime Demo")
    print("=" * 72)


def run_single_query(
    query: str,
    *,
    image_refs: list[str] | None = None,
    video_refs: list[str] | None = None,
    user_id: str = "demo_user",
    session_id: str = "demo_session",
    server: str = DEFAULT_SERVER,
    event_sink: "RecordingConsoleEventSink | None" = None,
    runtime_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one query against a remote backend and return a cli_payload-shaped dict."""

    final_response, final_error, events = run_remote_assistant_query(
        query,
        image_refs=list(image_refs or []),
        video_refs=list(video_refs or []),
        user_id=user_id,
        session_id=session_id,
        server=server,
        event_sink=event_sink,
    )
    if final_response is not None:
        return adapt_remote_response_to_cli_payload(
            final_response, query=query, events=events, runtime_info=runtime_info
        )
    return adapt_agent_error_to_cli_payload(
        final_error, query=query, events=events, runtime_info=runtime_info
    )


def build_ws_url(
    server: str,
    *,
    session_id: str,
    query: str,
    user_id: str,
    image_refs: list[str],
    video_refs: list[str],
) -> str:
    """Build the ws(s):// URL for /ws/agent/{session_id}, preserving any base path."""

    parts = urlsplit(server)
    if parts.scheme not in {"http", "https"}:
        raise RemoteServerError(f"Unsupported server URL scheme: {server!r} (use http/https).")
    if not parts.netloc:
        raise RemoteServerError(f"Invalid server URL: {server!r}.")
    ws_scheme = "wss" if parts.scheme == "https" else "ws"
    base_path = parts.path.rstrip("/")
    path = f"{base_path}/ws/agent/{quote(session_id, safe='')}"
    params: list[tuple[str, str]] = [("text", query), ("user_id", user_id), ("client", "cli")]
    params.extend(("image_id", ref) for ref in image_refs)
    params.extend(("video_id", ref) for ref in video_refs)
    return urlunsplit((ws_scheme, parts.netloc, path, urlencode(params, doseq=True), ""))


def fetch_runtime_info(server: str, *, timeout: float = 5.0) -> dict[str, Any]:
    """GET {server}/demo/runtime-info. Raise RemoteServerError on any failure."""

    url = server.rstrip("/") + "/demo/runtime-info"
    try:
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            response = client.get(url)
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise RemoteServerError(f"Could not reach Assistant server at {server}: {exc}") from exc
    return data if isinstance(data, dict) else {}


def run_remote_assistant_query(
    query: str,
    *,
    image_refs: list[str],
    video_refs: list[str],
    user_id: str,
    session_id: str,
    server: str,
    event_sink: "RecordingConsoleEventSink | None",
    open_timeout: float = 10.0,
) -> tuple[dict[str, Any] | None, object | None, list[AgentEvent]]:
    """Stream a run over the WebSocket endpoint.

    Returns (final_response, final_error, events). Exactly one of final_response /
    final_error is set on a completed stream.
    """

    ws_url = build_ws_url(
        server,
        session_id=session_id,
        query=query,
        user_id=user_id,
        image_refs=image_refs,
        video_refs=video_refs,
    )
    events: list[AgentEvent] = []
    final_response: dict[str, Any] | None = None
    final_error: object | None = None
    try:
        with ws_connect(ws_url, open_timeout=open_timeout, proxy=None) as websocket:
            for raw in websocket:
                try:
                    event = AgentEvent.model_validate(json.loads(raw))
                except (ValueError, TypeError) as exc:
                    raise RemoteServerError(f"Server sent an invalid event: {exc}") from exc
                events.append(event)
                if event_sink is not None:
                    event_sink.emit(event)
                if event.type == "agent_response":
                    response = event.payload.get("response") if isinstance(event.payload, dict) else None
                    if isinstance(response, dict):
                        final_response = response
                    break
                if event.type == "agent_error":
                    final_error = event.error
                    break
    except (OSError, RemoteServerError) as exc:
        if isinstance(exc, RemoteServerError):
            raise
        raise RemoteServerError(
            f"Could not connect to Assistant server at {server}: {exc}"
        ) from exc
    except Exception as exc:  # websockets raises its own exception hierarchy
        raise RemoteServerError(
            f"WebSocket request to {server} failed: {exc}"
        ) from exc
    if final_response is None and final_error is None:
        raise RemoteServerError(
            f"Server at {server} closed the stream without a final response."
        )
    return final_response, final_error, events


def _runtime_field(response: dict[str, Any], runtime_info: dict[str, Any] | None, key: str) -> Any:
    info = response.get("runtime_info") if isinstance(response.get("runtime_info"), dict) else None
    if info and info.get(key) is not None:
        return info[key]
    if runtime_info and runtime_info.get(key) is not None:
        return runtime_info[key]
    return ""


def _chat_provider(response: dict[str, Any], runtime_info: dict[str, Any] | None) -> str:
    for source in (response.get("runtime_info"), runtime_info):
        if isinstance(source, dict):
            providers = source.get("providers")
            if isinstance(providers, dict) and providers.get("chat"):
                return providers["chat"]
    return ""


def adapt_remote_response_to_cli_payload(
    response: dict[str, Any],
    *,
    query: str,
    events: list[AgentEvent],
    runtime_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Convert a remote AgentRunResponse + streamed events into the cli_payload shape."""

    tool_calls = response.get("tool_calls") or []
    failed = response.get("status") == "failed" or bool(response.get("errors"))
    return {
        "status": "failed" if failed else "success",
        "provider": _chat_provider(response, runtime_info),
        "model": "",
        "runtime_profile": _runtime_field(response, runtime_info, "runtime_profile"),
        "graph_mode": _runtime_field(response, runtime_info, "graph_mode"),
        "query": query,
        "response_text": response.get("response_text", ""),
        "response_data": response.get("data") or {},
        "tool_sequence": [call.get("tool_name") for call in tool_calls if call.get("tool_name")],
        "tool_calls": [
            {
                "tool_name": call.get("tool_name"),
                "status": call.get("status"),
                "output_ref": call.get("output_ref"),
                "error": call.get("error_message"),
            }
            for call in tool_calls
        ],
        "tool_results": response.get("tool_results") or [],
        "react_steps": response.get("react_steps") or [],
        "decision_trace": response.get("decision_trace") or [],
        "events": [event.model_dump(mode="json", exclude_none=True) for event in events],
        "trace": {},
        "errors": response.get("errors") or [],
        "run_id": response.get("run_id", ""),
        "trace_id": response.get("trace_id", ""),
        "runtime_info": response.get("runtime_info") or runtime_info or {},
        "current_stage": response.get("current_stage"),
        "blocked_reason": response.get("blocked_reason"),
    }


def adapt_agent_error_to_cli_payload(
    error: object,
    *,
    query: str,
    events: list[AgentEvent],
    runtime_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a failed cli_payload from a streamed agent_error (dict or string)."""

    if isinstance(error, dict):
        errors = [error]
    elif error:
        errors = [{"code": "TASK_FAILED", "message": str(error), "detail": {}, "recoverable": False}]
    else:
        errors = [{"code": "TASK_FAILED", "message": "run failed", "detail": {}, "recoverable": False}]
    run_id = next((event.run_id for event in events if event.run_id), "")
    return {
        "status": "failed",
        "provider": (runtime_info or {}).get("providers", {}).get("chat", "") if runtime_info else "",
        "model": "",
        "runtime_profile": (runtime_info or {}).get("runtime_profile", ""),
        "graph_mode": (runtime_info or {}).get("graph_mode", ""),
        "query": query,
        "response_text": "",
        "response_data": {},
        "tool_sequence": [],
        "tool_calls": [],
        "tool_results": [],
        "react_steps": [],
        "decision_trace": [],
        "events": [event.model_dump(mode="json", exclude_none=True) for event in events],
        "trace": {},
        "errors": errors,
        "run_id": run_id,
        "trace_id": "",
        "runtime_info": runtime_info or {},
        "current_stage": "failed",
        "blocked_reason": errors[0].get("message"),
    }


def print_remote_runtime_info(server: str, info: dict[str, Any]) -> None:
    providers = info.get("providers") or {}
    print()
    print("Config")
    print(f"  server: {server}")
    print(f"  runtime_profile: {info.get('runtime_profile', '(unknown)')}")
    print(f"  graph_mode: {info.get('graph_mode', '(unknown)')}")
    print(f"  chat_provider: {providers.get('chat', '(unknown)')}")
    print(f"  vision_provider: {providers.get('vision', '(unknown)')}")
    print(f"  image_provider: {providers.get('image_generation', '(unknown)')}")
    print(f"  video_provider: {providers.get('video', '(unknown)')}")
    print(f"  offline_default: {info.get('offline_default', '(unknown)')}")


def print_run(payload: dict[str, Any], *, show_trace: bool = False, live_timeline_printed: bool = False) -> None:
    if not live_timeline_printed:
        print()
        print("Timeline")
        for line in _timeline_from_payload(payload):
            print(line)
    if show_trace:
        _print_decision_trace(payload)
    _print_run_summary(payload)


def _print_decision_trace(payload: dict[str, Any]) -> None:
    print()
    print("Decision Trace")
    steps = payload.get("decision_trace") or []
    if not steps:
        print("  (no decision trace recorded)")
    for step in steps:
        if step.get("event") == "decision":
            print(f"  [{step['iteration']}] decision: {step.get('decision_type')}")
            if step.get("decision_summary"):
                print(f"      decision_summary: {step['decision_summary']}")
            if step.get("action"):
                print(f"      action: {step['action']}")
                print(f"      action_input: {json.dumps(step.get('action_input') or {}, ensure_ascii=False)}")
        elif step.get("event") == "observation":
            print(f"  [{step['iteration']}] observation: {step.get('action')}")
            print(f"      success: {step.get('success')}")
            if step.get("output_ref"):
                print(f"      output_ref: {_safe_display_value(step['output_ref'])}")
            if step.get("error"):
                print(f"      error: {json.dumps(step['error'], ensure_ascii=False)}")
            if step.get("recovery_hint"):
                print(f"      recovery_hint: {step['recovery_hint']}")
        elif step.get("event") == "final_answer":
            print(f"  [{step['iteration']}] final_answer")
            if step.get("decision_summary"):
                print(f"      decision_summary: {step['decision_summary']}")
            if step.get("answer"):
                print(f"      answer: {_safe_display_value(step['answer'])}")


def _print_run_summary(payload: dict[str, Any]) -> None:
    print()
    print("Run")
    print(f"  status: {payload['status']}")
    print(f"  tools: {', '.join(payload.get('tool_sequence') or []) or '(none)'}")
    final_answer_source = (payload.get("response_data") or {}).get("final_answer_source")
    if final_answer_source:
        print(f"  final_answer_source: {final_answer_source}")
    print(f"  run_id: {payload['run_id']}")
    print(f"  trace_id: {payload['trace_id']}")
    print()
    print("Tool Results")
    tool_results = payload.get("tool_results") or []
    tool_calls = payload.get("tool_calls") or []
    if tool_results:
        for index, result in enumerate(tool_results, start=1):
            latency = _format_latency(result.get("latency_ms"))
            suffix = f" | {latency}" if latency else ""
            print(f"  [{index}] {result.get('tool_name')} | success={result.get('success')}{suffix}")
            if result.get("output_ref"):
                print(f"      artifact: {_safe_display_value(result['output_ref'])}")
            summary = _compact_tool_result_summary(result.get("data") or {}, response_text=payload.get("response_text"))
            if summary:
                summary_lines = summary.splitlines()
                print(f"      summary: {summary_lines[0]}")
                for line in summary_lines[1:]:
                    print(f"        {line}")
            if result.get("error"):
                print(f"      error: {result['error']}")
    elif tool_calls:
        for index, call in enumerate(tool_calls, start=1):
            print(f"  [{index}] {call.get('tool_name')} | status={call.get('status')}")
            if call.get("output_ref"):
                print(f"      artifact: {_safe_display_value(call['output_ref'])}")
            if call.get("error"):
                print(f"      error: {call['error']}")
    else:
        print("  (none)")
    if not (payload.get("decision_trace") and any(step.get("event") == "final_answer" for step in payload["decision_trace"])):
        print()
        print("Final Answer")
        print(_safe_display_value(payload.get("response_text") or "(empty)"))
    if payload.get("errors"):
        print()
        print("Errors")
        print(json.dumps(payload["errors"], ensure_ascii=False, indent=2))
        print()
        print("Recovery hints")
        for hint in recovery_hints(payload["errors"]):
            print(f"  - {hint}")


def show_examples() -> None:
    print()
    print("Examples")
    for example in get_demo_examples():
        print(f"  - {example}")


def interactive_mode(args: argparse.Namespace, *, runtime_info: dict[str, Any] | None = None) -> int:
    show_examples()
    print()
    print("Type 'quit'/'exit' to quit, 'examples' to show examples.")
    while True:
        try:
            query = input("\nQuery> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not query:
            continue
        if query.lower() in {"quit", "exit", "q"}:
            return 0
        if query.lower() in {"examples", "example", "e"}:
            show_examples()
            continue
        event_sink = _event_sink(args)
        try:
            payload = run_single_query(
                query,
                image_refs=args.image_ref,
                video_refs=args.video_ref,
                user_id=args.user_id,
                session_id=args.session_id,
                server=args.server,
                event_sink=event_sink,
                runtime_info=runtime_info,
            )
        except RemoteServerError as exc:
            print()
            print(str(exc))
            continue
        _attach_replay_metadata(payload, args=args, query=query)
        print_run(payload, show_trace=args.show_trace, live_timeline_printed=event_sink.printed_timeline)
        _maybe_save_log(payload, args.save_log)


def _print_server_unreachable(server: str, message: str, *, as_json: bool) -> None:
    if as_json:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": "server_unavailable",
                    "message": message,
                    "server": server,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print()
        print("server_unavailable")
        print(f"  {message}")
        print("  Start the backend first, for example:")
        print("    python scripts/run_server.py --provider mock --image-provider mock")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.replay_log:
        replay = _load_replay_log(args.replay_log)
        args.query = [replay["query"]]
        args.image_ref = replay.get("image_refs", [])
        args.video_ref = replay.get("video_refs", [])
        args.user_id = replay.get("user_id", args.user_id)
        args.session_id = replay.get("session_id", args.session_id)

    runtime_info: dict[str, Any] | None = None
    try:
        runtime_info = fetch_runtime_info(args.server)
    except RemoteServerError as exc:
        _print_server_unreachable(args.server, str(exc), as_json=args.json)
        return 2

    if not args.json:
        print_header()
        print_remote_runtime_info(args.server, runtime_info)

    if not args.query:
        return interactive_mode(args, runtime_info=runtime_info)

    query = " ".join(args.query)
    if not args.json:
        print()
        print("Query")
        print(f"  {query}")
    event_sink = None if args.json else _event_sink(args)
    try:
        payload = run_single_query(
            query,
            image_refs=args.image_ref,
            video_refs=args.video_ref,
            user_id=args.user_id,
            session_id=args.session_id,
            server=args.server,
            event_sink=event_sink,
            runtime_info=runtime_info,
        )
    except RemoteServerError as exc:
        _print_server_unreachable(args.server, str(exc), as_json=args.json)
        return 2
    _attach_replay_metadata(payload, args=args, query=query)
    if args.json:
        print(_json_dumps(payload))
    else:
        print_run(
            payload,
            show_trace=args.show_trace,
            live_timeline_printed=bool(event_sink and event_sink.printed_timeline),
        )
        _maybe_save_log(payload, args.save_log)
    return 1 if payload.get("status") == "failed" else 0


class RecordingConsoleEventSink:
    """Record runtime events and optionally print them live."""

    def __init__(self, *, print_live: bool = True, mode: str = "timeline") -> None:
        self.print_live = print_live
        self.mode = mode
        self.printed_timeline = False
        self.events: list[AgentEvent] = []

    def emit(self, event: AgentEvent) -> None:
        self.events.append(event)
        if self.print_live:
            text = _format_live_event(event) if self.mode == "debug" else _format_timeline_event(event)
            if text:
                self.printed_timeline = True
                print(text, flush=True)


def _format_live_event(event: AgentEvent) -> str:
    trace = event.payload.get("decision_trace") if isinstance(event.payload, dict) else None
    if isinstance(trace, dict):
        return _format_live_decision_trace(event.type, trace)
    parts = [
        "event",
        event.type,
        f"run_id={event.run_id}" if event.run_id else None,
        f"node={event.node_name}" if event.node_name else None,
        f"tool={event.tool_name}" if event.tool_name else None,
        f"output_ref={event.output_ref}" if event.output_ref else None,
    ]
    if event.error:
        if isinstance(event.error, dict):
            parts.append(f"error={event.error.get('message') or event.error.get('code')}")
        else:
            parts.append(f"error={event.error}")
    return " | ".join(part for part in parts if part)


def _format_live_decision_trace(event_type: str, trace: dict[str, Any]) -> str:
    parts = ["trace", event_type, f"iteration={trace.get('iteration')}", f"event={trace.get('event')}"]
    if trace.get("decision_type"):
        parts.append(f"decision_type={trace['decision_type']}")
    if trace.get("decision_summary"):
        parts.append(f"decision_summary={trace['decision_summary']}")
    if trace.get("action"):
        parts.append(f"action={trace['action']}")
    if trace.get("success") is not None:
        parts.append(f"success={trace['success']}")
    if trace.get("output_ref"):
        parts.append(f"output_ref={_safe_display_value(trace['output_ref'])}")
    if trace.get("error"):
        error = trace["error"]
        parts.append(f"error={error.get('message') if isinstance(error, dict) else error}")
    if trace.get("answer"):
        parts.append(f"answer={_safe_display_value(trace['answer'])}")
    return " | ".join(str(part) for part in parts if part is not None)


def _event_sink(args: argparse.Namespace) -> RecordingConsoleEventSink:
    return RecordingConsoleEventSink(
        print_live=not args.no_live_events,
        mode="debug" if args.debug_events else "timeline",
    )


def _format_timeline_event(event: AgentEvent) -> str:
    trace = event.payload.get("decision_trace") if isinstance(event.payload, dict) else None
    if event.type == "task_started":
        return f"[run] started {event.run_id}"
    if event.type == "tool_started" and event.tool_name:
        return f"[tool:{event.tool_name}] running..."
    if isinstance(trace, dict):
        return _format_timeline_trace(trace)
    if event.type == "task_failed":
        message = _event_error_message(event.error)
        return f"[run] failed\n       error: {message}" if message else "[run] failed"
    if event.type == "agent_error":
        message = _event_error_message(event.error)
        return f"[error] {message}" if message else "[error] agent failed"
    return ""


def _format_timeline_trace(trace: dict[str, Any]) -> str:
    event_name = trace.get("event")
    if event_name == "decision":
        lines = [f"[plan] {trace.get('action') or trace.get('decision_type') or 'decision'}"]
        if trace.get("decision_summary"):
            lines.append(f"       reason: {trace['decision_summary']}")
        action_input = trace.get("action_input")
        if isinstance(action_input, dict) and action_input:
            lines.append(f"       input: {_compact_json(_public_action_input(action_input))}")
        return "\n".join(lines)
    if event_name == "observation":
        action = trace.get("action") or "tool"
        status = "succeeded" if trace.get("success") else "failed"
        lines = [f"[tool:{action}] {status}"]
        if trace.get("output_ref"):
            lines.append(f"       artifact: {_safe_display_value(trace['output_ref'])}")
        if trace.get("error"):
            lines.append(f"       error: {_event_error_message(trace['error'])}")
        if trace.get("recovery_hint"):
            lines.append(f"       recovery: {trace['recovery_hint']}")
        return "\n".join(lines)
    if event_name == "final_answer":
        answer = _safe_display_value(trace.get("answer") or "")
        return f"[answer]\n{answer}" if answer else "[answer]"
    return ""


def _timeline_from_payload(payload: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    if payload.get("query"):
        lines.extend(["Query", f"  {payload['query']}", ""])
    for step in payload.get("decision_trace") or []:
        formatted = _format_timeline_trace(step)
        if formatted:
            lines.append(formatted)
    if not lines and payload.get("response_text"):
        lines.append(f"[answer]\n{_safe_display_value(payload['response_text'])}")
    return lines


def _compact_json(value: dict[str, Any], *, max_length: int = 360) -> str:
    text = json.dumps(value, ensure_ascii=False)
    if len(text) <= max_length:
        return text
    return text[: max_length - 3] + "..."


def _public_action_input(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item
        for key, item in value.items()
        if item not in (None, "", [], {})
        and key
        not in {
            "memory_context",
            "user_id",
            "session_id",
            "product_info",
            "reference_image_ids",
        }
    }


def _event_error_message(error: object) -> str:
    if isinstance(error, dict):
        message = error.get("message") or error.get("code") or json.dumps(error, ensure_ascii=False)
        return _safe_display_value(str(message))
    return _safe_display_value(str(error)) if error else ""


def _attach_replay_metadata(payload: dict[str, Any], *, args: argparse.Namespace, query: str) -> None:
    payload["demo_metadata"] = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "script": "scripts/run_client.py",
        "server": getattr(args, "server", DEFAULT_SERVER),
        "replay_command": _replay_command_placeholder(payload),
        "request": {
            "query": query,
            "image_refs": list(args.image_ref or []),
            "video_refs": list(args.video_ref or []),
            "user_id": args.user_id,
            "session_id": args.session_id,
        },
    }


def _maybe_save_log(payload: dict[str, Any], path_value: str | None) -> None:
    if not path_value:
        return
    path = Path(path_value)
    if path.suffix:
        target = path
    else:
        target = path / f"{payload.get('run_id', 'run')}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_json_dumps(payload) + "\n", encoding="utf-8")
    print()
    print(f"Saved run log: {target}")
    print(f"Replay command: python scripts/run_client.py --replay-log {target}")


def _load_replay_log(path_value: str) -> dict[str, Any]:
    payload = json.loads(Path(path_value).read_text(encoding="utf-8"))
    request = (payload.get("demo_metadata") or {}).get("request") or {}
    query = request.get("query") or payload.get("query")
    if not query:
        raise SystemExit(f"Replay log does not contain a query: {path_value}")
    return {
        "query": str(query),
        "image_refs": list(request.get("image_refs") or []),
        "video_refs": list(request.get("video_refs") or []),
        "user_id": str(request.get("user_id") or "demo_user"),
        "session_id": str(request.get("session_id") or "demo_session"),
    }


def _replay_command_placeholder(payload: dict[str, Any]) -> str:
    run_id = payload.get("run_id") or "<run_id>"
    return f"python scripts/run_client.py --replay-log .local/demo_runs/{run_id}.json"


def _format_latency(value: object) -> str:
    if value is None:
        return ""
    try:
        return f"latency_ms={int(value)}"
    except (TypeError, ValueError):
        return ""


def _compact_tool_result_summary(data: dict[str, Any], *, response_text: object | None = None) -> str:
    response = _safe_display_value(response_text).strip() if response_text else ""
    product_summary = _compact_product_result_summary(data)
    if product_summary:
        return product_summary
    for key in ("summary", "response_text", "image_url", "output_ref", "request_id"):
        value = data.get(key)
        if value:
            summary = _safe_display_value(str(value)).strip()
            if summary and summary != response:
                return summary[:240]
            return ""
    image_urls = data.get("image_urls")
    if isinstance(image_urls, list) and image_urls:
        return _safe_display_value(str(image_urls[0]))[:240]
    contract = data.get("contract")
    if isinstance(contract, dict):
        return f"capability={contract.get('capability')}, status={contract.get('status')}"
    return ""


def _compact_product_result_summary(data: dict[str, Any]) -> str:
    items = data.get("items")
    if isinstance(items, list) and items:
        lines = []
        total = data.get("total")
        for index, item in enumerate(items[:5], start=1):
            if isinstance(item, dict):
                lines.append(_format_compact_product_item(item, index=index))
        if lines:
            header = f"showing {len(lines)} of {total}" if total is not None else f"showing {len(lines)}"
            return "\n".join([header, *lines])
    best_offer = data.get("best_offer")
    if isinstance(best_offer, dict) and best_offer:
        return _format_compact_product_item(best_offer, prefix="best")
    return ""


def _format_compact_product_item(
    item: dict[str, Any],
    *,
    index: int | None = None,
    prefix: str = "top",
) -> str:
    title = _safe_display_value(item.get("title") or "candidate")
    price = item.get("total_price") or item.get("price")
    currency = item.get("currency") or "CNY"
    url = item.get("product_url") or item.get("url")
    label = f"{index}" if index is not None else prefix
    price_part = f", price={price} {currency}" if price is not None else ""
    url_part = f", url={_safe_display_value(url)}" if url else ""
    return f"{label}: {title}{price_part}{url_part}"


def recovery_hints(errors: list[dict[str, Any]]) -> list[str]:
    hints: list[str] = []
    for error in errors:
        code = str(error.get("code") or "")
        message = str(error.get("message") or "")
        if code == "PROVIDER_UNCONFIGURED":
            hints.append("检查 .env 中对应 provider 的 API Key、Base URL 和模型名。")
        elif code == "PROVIDER_AUTH_FAILED":
            hints.append("检查 API Key 是否属于当前 provider，是否有权限，且没有多余引号或空格。")
        elif code == "PROVIDER_TIMEOUT":
            hints.append("真实 Provider 响应超时；可降低生成尺寸、换模型，或稍后重试。")
        elif code == "PROVIDER_RATE_LIMITED":
            hints.append("Provider 限流；等待一段时间或降低请求频率。")
        elif code in {"PROVIDER_UNAVAILABLE", "TASK_FAILED"} and "size" in message.lower():
            hints.append("检查图像尺寸格式和模型支持的尺寸，例如 DashScope 使用 1024*1024。")
        elif code == "TOOL_INPUT_INVALID":
            hints.append("检查 ReAct action_input 是否缺少工具必需字段。")
    return hints or ["查看上方 error code/message、tool input 和 trace_id；必要时用 --save-log 保存后复现。"]


def _json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _safe_display_value(value: object) -> str:
    text = str(value)
    sensitive_params = ("X-Tos-Credential=", "X-Tos-Signature=", "X-Tos-Algorithm=")
    if any(param in text for param in sensitive_params):
        return text.split("?", 1)[0] + "?[signed-url-redacted]"
    return text


if __name__ == "__main__":
    raise SystemExit(main())
