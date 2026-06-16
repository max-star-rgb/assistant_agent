"""Manual smoke entry point for render_3d capability."""

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


RENDER_PROVIDER_REQUIREMENTS = {
    "http": "RENDER_BASE_URL and RENDER_API_KEY",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a manual render_3d smoke test. Defaults use the offline mock adapter.",
    )
    parser.add_argument("--scene", required=True, help="Scene description for render_3d.")
    parser.add_argument("--product", default=None, help="Optional product/object text to include in the scene.")
    parser.add_argument("--user-id", default="smoke_user", help="Local smoke user id.")
    parser.add_argument("--session-id", default="smoke_session", help="Local smoke session id.")
    return parser


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source = os.environ if env is None else env
    provider = source.get("MULTIMODAL_AGENT_RENDER_PROVIDER", "mock")

    missing = _missing_provider_config(provider, source)
    if missing:
        _print_provider_unconfigured(missing)
        return 2

    config = ProviderConfig.from_env(source)
    request = UserRequest(
        user_id=args.user_id,
        session_id=args.session_id,
        text=_build_render_text(args.scene, args.product),
    )
    state = AgentGraphRuntime(config=config).run_state(request)
    output = {
        "status": "success" if state.status != "failed" else "failed",
        "provider": provider,
        "capability": "render_3d",
        "intent": state.intent.intent if state.intent else None,
        "response_text": state.response.message if state.response else "",
        "tool_calls": [
            {"tool_name": call.tool_name, "status": call.status, "output_ref": call.output_ref}
            for call in state.tool_calls
        ],
        "render_result": _render_result_payload(state),
        "errors": [_api_error_payload(error) for error in state.errors],
        "run_id": state.run_id,
        "trace_id": state.trace_id,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 1 if state.status == "failed" else 0


def _build_render_text(scene: str, product: str | None) -> str:
    if product:
        return f"把{product}放到{scene}里看看"
    return scene


def _missing_provider_config(provider: str, source: Mapping[str, str]) -> str | None:
    if provider == "http":
        missing = []
        if not source.get("RENDER_BASE_URL"):
            missing.append("RENDER_BASE_URL")
        if not source.get("RENDER_API_KEY"):
            missing.append("RENDER_API_KEY")
        if missing:
            return f"missing {', '.join(missing)}"
    if provider not in {"mock", *RENDER_PROVIDER_REQUIREMENTS}:
        return "MULTIMODAL_AGENT_RENDER_PROVIDER must be mock or http."
    return None


def _render_result_payload(state: Any) -> dict[str, Any] | None:
    for result in state.tool_results:
        if result.tool_name == "render_3d" and result.success:
            data = result.data or {}
            return {
                "status": data.get("status"),
                "provider": data.get("provider"),
                "output_ref": result.output_ref,
                "preview_url": data.get("preview_url"),
                "model_url": data.get("model_url"),
                "render_id": data.get("render_id"),
                "scene_description": data.get("scene_description"),
                "used_inputs": data.get("used_inputs"),
            }
    return None


def _api_error_payload(error: Any) -> dict[str, Any]:
    return api_error_from_agent_error(error).model_dump(mode="json")


def _print_provider_unconfigured(reason: str) -> None:
    print("provider_unconfigured")
    print(reason)
    print("Please set MULTIMODAL_AGENT_RENDER_PROVIDER and the required render provider configuration.")


if __name__ == "__main__":
    raise SystemExit(main())
