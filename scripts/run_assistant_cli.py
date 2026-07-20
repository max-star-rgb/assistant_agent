"""Run the assistant agent from a local offline CLI.

This runs the agent IN-PROCESS for offline development/smoke. It does NOT
connect to a backend server. For server-backed realtime smoke, use
`scripts/realtime_media_client.py` or `scripts/run_gateway_client.py` against
`scripts/run_server.py`.
"""

from __future__ import annotations

import argparse
import asyncio
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

from assistant_agent.config import ProviderConfig
from assistant_agent.gateway import GatewaySessionManager
from assistant_agent.realtime import GatewayAgentAdapter
from assistant_agent.schemas.api import api_error_from_agent_error
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.assistant_run_service import create_runtime
from assistant_agent.services.assistant_runtime_app import AssistantRuntimeApp
from assistant_agent.services.gateway_turn_facade import GatewayTurnFacade, GatewayTurnRequest

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
    return asyncio.run(
        _run_text_prompt_through_gateway(
            text,
            image_refs=image_refs,
            video_refs=video_refs,
            user_id=user_id,
            session_id=session_id,
        )
    )


async def _run_text_prompt_through_gateway(
    text: str,
    *,
    image_refs: list[str] | None = None,
    video_refs: list[str] | None = None,
    user_id: str = "cli_user",
    session_id: str = "cli_session",
) -> dict[str, Any]:
    app = AssistantRuntimeApp(
        runtime_factory=lambda: create_runtime(config=ProviderConfig(), load_env=False)
    )
    captured: list[Any] = []

    def run_request(request: UserRequest, **kwargs: Any) -> Any:
        artifacts = app.run_request(request, **kwargs)
        captured.append(artifacts)
        return artifacts

    manager = GatewaySessionManager(
        backend_factory=lambda: GatewayAgentAdapter(
            run_request=run_request,
            load_env=False,
        ),
        start_reaper=False,
    )
    facade = GatewayTurnFacade(manager=manager)
    try:
        await facade.run_turn(
            GatewayTurnRequest(
                user_id=user_id,
                session_id=session_id,
                text=text,
                image_ids=list(image_refs or []),
                video_ids=list(video_refs or []),
                metadata={
                    "offline": True,
                    "gateway": {"suppress_realtime_backend_source": True},
                },
            )
        )
    finally:
        await manager.close()

    if not captured:
        raise ValueError("Gateway CLI run completed without assistant artifacts.")
    return _payload_from_artifacts(captured[-1])


def _payload_from_artifacts(artifacts: Any) -> dict[str, Any]:
    state = artifacts.state
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
