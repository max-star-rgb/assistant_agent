#!/usr/bin/env python3
"""Interactive ReAct assistant loop demo.

This script is intentionally manual-smoke oriented. It can load local `.env`
configuration and run the LangGraph Assistant Loop with a real chat provider,
while keeping default tests offline.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from multimodal_agent.config import ProviderConfig
from multimodal_agent.schemas.events import AgentEvent
from multimodal_agent.services.assistant_run_service import load_env_file, run_assistant_query
from multimodal_agent.services.event_sink import EventSink
from multimodal_agent.services.provider_specs import (
    resolve_chat_provider,
    supported_chat_providers,
    supported_image_generation_providers,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the ReAct assistant loop. Defaults load .env and keep provider selection explicit.",
    )
    parser.add_argument("query", nargs="*", help="Optional query. Omit for interactive mode.")
    parser.add_argument("--env-file", default=".env", help="Env file to load before running.")
    parser.add_argument("--no-env-file", action="store_true", help="Do not load an env file.")
    parser.add_argument(
        "--provider",
        choices=supported_chat_providers(),
        help="Override MULTIMODAL_AGENT_CHAT_PROVIDER for this process.",
    )
    parser.add_argument(
        "--image-provider",
        choices=supported_image_generation_providers(),
        help="Override MULTIMODAL_AGENT_IMAGE_PROVIDER for this process.",
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
    config: ProviderConfig | None = None,
    event_sink: EventSink | None = None,
) -> dict[str, Any]:
    return run_assistant_query(
        query,
        image_refs=image_refs,
        video_refs=video_refs,
        user_id=user_id,
        session_id=session_id,
        config=config or ProviderConfig.from_env(),
        event_sink=event_sink,
        load_env=False,
        metadata={"source": "demo_assistant_loop"},
    ).cli_payload()


def print_config(config: ProviderConfig, *, loaded_env_keys: list[str]) -> None:
    chat = resolve_chat_provider(config.chat_provider, os.environ)
    missing = chat.missing_required_env()
    print()
    print("Config")
    print(f"  env_file_keys_loaded: {len(loaded_env_keys)}")
    print(f"  runtime_profile: {config.runtime_profile.name}")
    print(f"  graph_mode: {config.agent_graph_mode}")
    print(f"  chat_provider: {config.chat_provider}")
    print(f"  chat_model: {config.chat_model or '(unset)'}")
    print(f"  chat_base_url: {_redact_url(config.chat_base_url)}")
    print(f"  image_provider: {config.image_generation_provider}")
    print(f"  image_model: {config.image_generation_model or '(unset)'}")
    print(f"  image_base_url: {_redact_url(config.image_generation_base_url)}")
    print(f"  video_provider: {config.video_provider}")
    print(f"  video_model: {config.video_understanding_model or '(unset)'}")
    print(f"  video_base_url: {_redact_url(config.video_understanding_base_url)}")
    print(f"  provider_ready: {'no, missing ' + ', '.join(missing) if missing else 'yes'}")
    print(f"  max_tool_iterations: {config.max_tool_iterations}")


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
                print(f"      summary: {summary}")
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
    for example in [
        "你好，请用两句话介绍你能做什么",
        "帮我写一段白色运动鞋的电商卖点文案",
        "生成一张白色运动鞋的电商主图，干净背景，真实摄影风格",
        "帮我找一款无线蓝牙耳机并比较价格",
        "图里是什么？请简要描述主要物体、颜色、材质和场景。",
    ]:
        print(f"  - {example}")


def interactive_mode(config: ProviderConfig, args: argparse.Namespace) -> int:
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
        payload = run_single_query(
            query,
            image_refs=args.image_ref,
            video_refs=args.video_ref,
            user_id=args.user_id,
            session_id=args.session_id,
            config=config,
            event_sink=event_sink,
        )
        _attach_replay_metadata(payload, args=args, query=query)
        print_run(payload, show_trace=args.show_trace, live_timeline_printed=event_sink.printed_timeline)
        _maybe_save_log(payload, args.save_log)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    loaded: dict[str, str] = {}
    if not args.no_env_file:
        loaded = load_env_file((REPO_ROOT / args.env_file).resolve())
    if args.provider:
        os.environ["MULTIMODAL_AGENT_RUNTIME_PROFILE"] = os.environ.get(
            "MULTIMODAL_AGENT_RUNTIME_PROFILE",
            "provider_smoke",
        )
        os.environ["MULTIMODAL_AGENT_CHAT_PROVIDER"] = args.provider
    _apply_demo_image_provider_default(args)

    if args.replay_log:
        replay = _load_replay_log(args.replay_log)
        args.query = [replay["query"]]
        args.image_ref = replay.get("image_refs", [])
        args.video_ref = replay.get("video_refs", [])
        args.user_id = replay.get("user_id", args.user_id)
        args.session_id = replay.get("session_id", args.session_id)

    config = ProviderConfig.from_env()
    if not args.json:
        print_header()
        print_config(config, loaded_env_keys=sorted(loaded))
    missing = _missing_chat_config(config, os.environ)
    if missing:
        if args.json:
            print(
                json.dumps(
                    {
                        "status": "failed",
                        "error": "provider_unconfigured",
                        "missing": missing,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print()
            print("provider_unconfigured")
            print(f"  missing: {', '.join(missing)}")
            print("  Set the required variables in .env or your shell before running real provider smoke.")
        return 2

    if not args.query:
        return interactive_mode(config, args)

    query = " ".join(args.query)
    if not args.json:
        print()
        print("Query")
        print(f"  {query}")
    event_sink = None if args.json else _event_sink(args)
    payload = run_single_query(
        query,
        image_refs=args.image_ref,
        video_refs=args.video_ref,
        user_id=args.user_id,
        session_id=args.session_id,
        config=config,
        event_sink=event_sink,
    )
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


def _redact_url(value: str | None) -> str:
    if not value:
        return "(unset)"
    return value.split("?", 1)[0]


def _missing_chat_config(config: ProviderConfig, source: Mapping[str, str]) -> list[str]:
    return resolve_chat_provider(config.chat_provider, source).missing_required_env()


def _apply_demo_image_provider_default(args: argparse.Namespace) -> None:
    if args.image_provider:
        os.environ["MULTIMODAL_AGENT_RUNTIME_PROFILE"] = os.environ.get(
            "MULTIMODAL_AGENT_RUNTIME_PROFILE",
            "provider_smoke",
        )
        os.environ["MULTIMODAL_AGENT_IMAGE_PROVIDER"] = args.image_provider


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
        "script": "scripts/demo_assistant_loop.py",
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
    print(f"Replay command: python scripts/demo_assistant_loop.py --replay-log {target}")


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
    return f"python scripts/demo_assistant_loop.py --replay-log .local/demo_runs/{run_id}.json"


def _format_latency(value: object) -> str:
    if value is None:
        return ""
    try:
        return f"latency_ms={int(value)}"
    except (TypeError, ValueError):
        return ""


def _compact_tool_result_summary(data: dict[str, Any], *, response_text: object | None = None) -> str:
    response = _safe_display_value(response_text).strip() if response_text else ""
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
