"""Manual smoke entry point for video_understanding capability."""

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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a manual video_understanding smoke test. Defaults use the offline mock adapter.",
    )
    parser.add_argument("--video-ref", default="mock://video/product-demo", help="Safe video reference or mock id.")
    parser.add_argument("--text", default="总结这个视频", help="User request text.")
    parser.add_argument("--user-id", default="smoke_user", help="Local smoke user id.")
    parser.add_argument("--session-id", default="smoke_session", help="Local smoke session id.")
    return parser


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source = os.environ if env is None else env
    provider = source.get("MULTIMODAL_AGENT_VIDEO_PROVIDER", "mock")

    missing = _missing_provider_config(provider, source)
    if missing:
        _print_provider_unconfigured(missing)
        return 2

    config = ProviderConfig.from_env(source)
    request = UserRequest(
        user_id=args.user_id,
        session_id=args.session_id,
        text=args.text,
        video_ids=[args.video_ref],
    )
    state = AgentGraphRuntime(config=config).run_state(request)
    output = {
        "status": "success" if state.status != "failed" else "failed",
        "provider": provider,
        "capability": "video_understanding",
        "intent": state.intent.intent if state.intent else None,
        "response_text": state.response.message if state.response else "",
        "tool_calls": [
            {"tool_name": call.tool_name, "status": call.status, "output_ref": call.output_ref}
            for call in state.tool_calls
        ],
        "video_result": _video_result_payload(state),
        "errors": [_api_error_payload(error) for error in state.errors],
        "run_id": state.run_id,
        "trace_id": state.trace_id,
    }
    print(json.dumps(_sanitize_payload(output), ensure_ascii=False, indent=2))
    return 1 if state.status == "failed" else 0


def _missing_provider_config(provider: str, source: Mapping[str, str]) -> str | None:
    if provider == "http":
        missing = []
        if not source.get("VIDEO_UNDERSTANDING_BASE_URL"):
            missing.append("VIDEO_UNDERSTANDING_BASE_URL")
        if not source.get("VIDEO_UNDERSTANDING_API_KEY"):
            missing.append("VIDEO_UNDERSTANDING_API_KEY")
        if missing:
            return f"missing {', '.join(missing)}"
    if provider == "ark":
        if not source.get("ARK_VISION_API_KEY"):
            return "missing ARK_VISION_API_KEY"
    if provider not in {"mock", "http", "ark"}:
        return "MULTIMODAL_AGENT_VIDEO_PROVIDER must be mock, http, or ark."
    return None


def _video_result_payload(state: Any) -> dict[str, Any] | None:
    for result in state.tool_results:
        if result.tool_name == "video_understanding" and result.success:
            data = result.data or {}
            return {
                "provider": data.get("provider"),
                "model": data.get("model"),
                "output_ref": result.output_ref,
                "summary": data.get("summary"),
                "objects": data.get("objects"),
                "products": data.get("products"),
                "scene": data.get("scene"),
            }
    return None


def _api_error_payload(error: Any) -> dict[str, Any]:
    return api_error_from_agent_error(error).model_dump(mode="json")


def _print_provider_unconfigured(reason: str) -> None:
    print("provider_unconfigured")
    print(reason)
    print("Please set MULTIMODAL_AGENT_VIDEO_PROVIDER and the required video provider configuration.")


def _sanitize_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _sanitize_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_payload(item) for item in value]
    if isinstance(value, str):
        lowered = value.lower()
        if "bearer " in lowered or "authorization" in lowered or "base64" in lowered:
            return "[redacted]"
    return value


if __name__ == "__main__":
    raise SystemExit(main())
