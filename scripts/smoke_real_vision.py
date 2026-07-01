"""Manual smoke test entry point for an explicitly configured real Vision Provider."""

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

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.api import api_error_from_agent_error
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.provider_specs import resolve_vision_provider, supported_vision_providers


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a manual real Vision Provider smoke test. Defaults never call a real provider.",
    )
    parser.add_argument("--image", required=True, help="Path to a low-risk local demo image.")
    parser.add_argument(
        "--question",
        default="图里是什么？请简要描述主要物体、颜色、材质和场景。",
        help="Question sent to the configured vision provider.",
    )
    parser.add_argument("--user-id", default="smoke_user", help="Local smoke user id.")
    parser.add_argument("--session-id", default="smoke_session", help="Local smoke session id.")
    return parser


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source = os.environ if env is None else env
    provider = source.get("MULTIMODAL_AGENT_VISION_PROVIDER", "mock")

    if provider not in supported_vision_providers() or provider == "mock":
        supported = ", ".join(name for name in supported_vision_providers() if name != "mock")
        _print_provider_unconfigured(f"MULTIMODAL_AGENT_VISION_PROVIDER must be set to one of: {supported}.")
        return 2

    missing = resolve_vision_provider(provider, source).missing_required_env()
    if missing:
        _print_provider_unconfigured(f"missing {', '.join(missing)}")
        return 2

    image_path = Path(args.image)
    if not image_path.exists() or not image_path.is_file():
        print(f"missing_demo_image: {image_path}")
        print("Please provide a low-risk local image, for example demo_data/images/shoe.jpg.")
        return 2

    config = ProviderConfig.from_env(source)
    request = UserRequest(
        user_id=args.user_id,
        session_id=args.session_id,
        text=args.question,
        image_ids=[str(image_path)],
    )
    state = AgentGraphRuntime(config=config).run_state(request)

    output = {
        "status": "success" if state.status != "failed" else "failed",
        "provider": provider,
        "intent": state.intent.intent if state.intent else None,
        "response_text": state.response.message if state.response else "",
        "tool_calls": [
            {"tool_name": call.tool_name, "status": call.status, "output_ref": call.output_ref}
            for call in state.tool_calls
        ],
        "vision_result": _vision_result_payload(state),
        "errors": [_api_error_payload(error) for error in state.errors],
        "run_id": state.run_id,
        "trace_id": state.trace_id,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 1 if state.status == "failed" else 0


def _api_error_payload(error: Any) -> dict[str, Any]:
    return api_error_from_agent_error(error).model_dump(mode="json")


def _vision_result_payload(state: Any) -> dict[str, Any] | None:
    for result in state.tool_results:
        if result.tool_name == "vision_understanding" and result.success:
            return result.data
    return None


def _print_provider_unconfigured(reason: str) -> None:
    print("provider_unconfigured")
    print(reason)
    print("Please set MULTIMODAL_AGENT_VISION_PROVIDER and provider API key.")


if __name__ == "__main__":
    raise SystemExit(main())
