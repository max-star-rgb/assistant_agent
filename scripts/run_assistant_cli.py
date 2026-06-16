"""Run the assistant agent from a local offline CLI."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from multimodal_agent.agent.runtime import AgentGraphRuntime
from multimodal_agent.config import ProviderConfig
from multimodal_agent.schemas.api import api_error_from_agent_error
from multimodal_agent.schemas.requests import UserRequest

from scripts.run_demo_flows import GENERIC_RESPONSE_TEXT, run_demo_flows


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the assistant locally. Defaults always use mock/local providers.",
    )
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--text", help="Text prompt to send to the assistant.")
    input_group.add_argument("--scenario", help="Scenario id from demo_data/scenarios/e2e_demo_scenarios.json.")
    parser.add_argument("--image-ref", action="append", default=[], help="Optional mock/local image reference.")
    parser.add_argument("--video-ref", action="append", default=[], help="Optional mock/local video reference.")
    parser.add_argument("--user-id", default="cli_user", help="User id used for the local run.")
    parser.add_argument("--session-id", default="cli_session", help="Session id used for the local run.")
    parser.add_argument(
        "--format",
        choices=("json", "text"),
        default="json",
        help="Output JSON or a readable text summary.",
    )
    return parser


def run_text_prompt(
    text: str,
    image_refs: list[str] | None = None,
    video_refs: list[str] | None = None,
    user_id: str = "cli_user",
    session_id: str = "cli_session",
) -> dict[str, Any]:
    runtime = AgentGraphRuntime(config=ProviderConfig())
    state = runtime.run_state(
        UserRequest(
            user_id=user_id,
            session_id=session_id,
            text=text,
            image_ids=list(image_refs or []),
            video_ids=list(video_refs or []),
            metadata={"source": "assistant_cli", "offline": True},
        )
    )
    response_text = state.response.message if state.response else ""
    tool_sequence = [call.tool_name for call in state.tool_calls]
    errors = [api_error_from_agent_error(error).model_dump(mode="json") for error in state.errors]
    status = "succeeded" if state.status != "failed" else "failed"
    return {
        "status": status,
        "intent": state.intent.intent if state.intent else None,
        "response_text": response_text,
        "tool_sequence": tool_sequence,
        "run_id": state.run_id,
        "trace_id": state.trace_id,
        "errors": errors,
        "offline": True,
        "checks": {
            "non_generic_response": bool(response_text and response_text != GENERIC_RESPONSE_TEXT),
        },
    }


def run_cli(args: argparse.Namespace) -> dict[str, Any]:
    if args.scenario:
        summary = run_demo_flows(scenario_id=args.scenario)
        result = dict(summary["results"][0])
        result["offline"] = True
        return result
    return run_text_prompt(
        text=args.text,
        image_refs=args.image_ref,
        video_refs=args.video_ref,
        user_id=args.user_id,
        session_id=args.session_id,
    )


def format_text(payload: dict[str, Any]) -> str:
    lines = [
        f"status: {payload.get('status')}",
        f"intent: {payload.get('intent')}",
        f"response_text: {payload.get('response_text')}",
        f"tool_sequence: {', '.join(payload.get('tool_sequence') or []) or '(none)'}",
        f"run_id: {payload.get('run_id')}",
        f"trace_id: {payload.get('trace_id')}",
        f"errors: {json.dumps(payload.get('errors', []), ensure_ascii=False)}",
        "offline: true",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = run_cli(args)
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2))
        return 2

    if args.format == "text":
        print(format_text(payload))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if payload.get("status") == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
