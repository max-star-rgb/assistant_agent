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
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from multimodal_agent.agent.runtime import AgentGraphRuntime
from multimodal_agent.config import ProviderConfig
from multimodal_agent.schemas.api import api_error_from_agent_error
from multimodal_agent.schemas.requests import UserRequest
from multimodal_agent.services.event_sink import ListEventSink
from multimodal_agent.services.provider_specs import resolve_chat_provider, supported_chat_providers
from multimodal_agent.services.trace_store import trace_debug_summary


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
    parser.add_argument("--image-ref", action="append", default=[], help="Optional image id/ref for the request.")
    parser.add_argument("--video-ref", action="append", default=[], help="Optional video id/ref for the request.")
    parser.add_argument("--user-id", default="demo_user", help="User id for this demo run.")
    parser.add_argument("--session-id", default="demo_session", help="Session id for this demo run.")
    parser.add_argument("--json", action="store_true", help="Print a machine-readable JSON payload.")
    return parser


def print_header() -> None:
    print()
    print("=" * 72)
    print("ReAct Assistant Loop - Runtime Demo")
    print("=" * 72)


def load_env_file(path: Path, *, override: bool = False) -> dict[str, str]:
    """Load dotenv-style KEY=VALUE pairs without adding a runtime dependency."""

    loaded: dict[str, str] = {}
    if not path.exists():
        return loaded
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key.startswith("export "):
            key = key.removeprefix("export ").strip()
        value = _strip_env_value(value.strip())
        loaded[key] = value
        if override or key not in os.environ:
            os.environ[key] = value
    return loaded


def run_single_query(
    query: str,
    *,
    image_refs: list[str] | None = None,
    video_refs: list[str] | None = None,
    user_id: str = "demo_user",
    session_id: str = "demo_session",
    config: ProviderConfig | None = None,
) -> dict[str, Any]:
    event_sink = ListEventSink()
    runtime = AgentGraphRuntime(config=config or ProviderConfig.from_env(), event_sink=event_sink)
    request = UserRequest(
        user_id=user_id,
        session_id=session_id,
        text=query,
        image_ids=list(image_refs or []),
        video_ids=list(video_refs or []),
        metadata={"source": "demo_assistant_loop"},
    )
    state = runtime.run_state(request)
    trace_summary = trace_debug_summary(runtime.trace_store.list_by_run(state.run_id))
    return {
        "status": "success" if state.status != "failed" else "failed",
        "provider": runtime.config.chat_provider,
        "model": runtime.config.chat_model,
        "runtime_profile": runtime.config.runtime_profile.name,
        "graph_mode": runtime.config.agent_graph_mode,
        "query": query,
        "response_text": state.response.message if state.response else "",
        "tool_sequence": [call.tool_name for call in state.tool_calls],
        "tool_calls": [
            {
                "tool_name": call.tool_name,
                "status": call.status,
                "output_ref": call.output_ref,
                "error": call.error_message,
            }
            for call in state.tool_calls
        ],
        "react_steps": _safe_steps(state.request.metadata.get("assistant_loop_steps")),
        "events": [
            event.model_dump(mode="json", exclude_none=True)
            for event in event_sink.events
        ],
        "trace": trace_summary,
        "errors": [api_error_from_agent_error(error).model_dump(mode="json") for error in state.errors],
        "run_id": state.run_id,
        "trace_id": state.trace_id,
    }


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
    print(f"  provider_ready: {'no, missing ' + ', '.join(missing) if missing else 'yes'}")
    print(f"  max_tool_iterations: {config.max_tool_iterations}")


def print_run(payload: dict[str, Any]) -> None:
    print()
    print(f"User: {payload['query']}")
    print("-" * 72)
    print("ReAct steps")
    steps = payload.get("react_steps") or []
    if not steps:
        print("  (no assistant_loop_steps recorded)")
    for step in steps:
        if "decision_type" in step:
            print(f"  [{step['iteration']}] thought/decision: {step['decision_type']}")
            if step.get("tool_name"):
                print(f"      action: {step['tool_name']}")
                print(f"      action_input: {json.dumps(step.get('tool_input') or {}, ensure_ascii=False)}")
            if step.get("reason"):
                print(f"      reason: {step['reason']}")
            if step.get("message") and step["decision_type"] != "tool_call":
                print(f"      answer: {step['message']}")
        else:
            print(f"  [{step['iteration']}] observation: {step.get('observation_tool')}")
            print(f"      success: {step.get('success')}")
            if step.get("output_ref"):
                print(f"      output_ref: {step['output_ref']}")
            if step.get("error"):
                print(f"      error: {step['error']}")
    print()
    print("Runtime")
    print(f"  status: {payload['status']}")
    print(f"  tool_sequence: {', '.join(payload.get('tool_sequence') or []) or '(none)'}")
    print(f"  trace_nodes: {', '.join(payload.get('trace', {}).get('node_path') or []) or '(none)'}")
    print(f"  run_id: {payload['run_id']}")
    print(f"  trace_id: {payload['trace_id']}")
    print()
    print("Final response")
    print(payload.get("response_text") or "(empty)")
    if payload.get("errors"):
        print()
        print("Errors")
        print(json.dumps(payload["errors"], ensure_ascii=False, indent=2))


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
        payload = run_single_query(
            query,
            image_refs=args.image_ref,
            video_refs=args.video_ref,
            user_id=args.user_id,
            session_id=args.session_id,
            config=config,
        )
        print_run(payload)


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

    config = ProviderConfig.from_env()
    print_header()
    print_config(config, loaded_env_keys=sorted(loaded))
    missing = _missing_chat_config(config, os.environ)
    if missing:
        print()
        print("provider_unconfigured")
        print(f"  missing: {', '.join(missing)}")
        print("  Set the required variables in .env or your shell before running real provider smoke.")
        return 2

    if not args.query:
        return interactive_mode(config, args)

    payload = run_single_query(
        " ".join(args.query),
        image_refs=args.image_ref,
        video_refs=args.video_ref,
        user_id=args.user_id,
        session_id=args.session_id,
        config=config,
    )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_run(payload)
    return 1 if payload.get("status") == "failed" else 0


def _strip_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    comment_index = value.find(" #")
    if comment_index >= 0:
        return value[:comment_index].strip()
    return value


def _redact_url(value: str | None) -> str:
    if not value:
        return "(unset)"
    return value.split("?", 1)[0]


def _safe_steps(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _missing_chat_config(config: ProviderConfig, source: Mapping[str, str]) -> list[str]:
    return resolve_chat_provider(config.chat_provider, source).missing_required_env()


if __name__ == "__main__":
    raise SystemExit(main())
