"""Manual smoke entry point for direct_chat text capability."""

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


REAL_PROVIDER_REQUIREMENTS = {
    "openai": "OPENAI_API_KEY",
    "qwen": "QWEN_API_KEY",
    "local": "LOCAL_CHAT_BASE_URL",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a manual direct_chat smoke test. Defaults use the offline mock adapter.",
    )
    parser.add_argument("--text", required=True, help="Text prompt for direct_chat.")
    parser.add_argument("--user-id", default="smoke_user", help="Local smoke user id.")
    parser.add_argument("--session-id", default="smoke_session", help="Local smoke session id.")
    return parser


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source = os.environ if env is None else env
    provider = source.get("MULTIMODAL_AGENT_CHAT_PROVIDER", "mock")

    missing = _missing_provider_config(provider, source)
    if missing:
        _print_provider_unconfigured(missing)
        return 2

    config = ProviderConfig.from_env(source)
    request = UserRequest(
        user_id=args.user_id,
        session_id=args.session_id,
        text=args.text,
    )
    state = AgentGraphRuntime(config=config).run_state(request)

    output = {
        "status": "success" if state.status != "failed" else "failed",
        "provider": provider,
        "capability": "direct_chat",
        "intent": state.intent.intent if state.intent else None,
        "response_text": state.response.message if state.response else "",
        "contract": state.response.data.get("contract") if state.response and state.response.data else None,
        "tool_calls": [
            {"tool_name": call.tool_name, "status": call.status, "output_ref": call.output_ref}
            for call in state.tool_calls
        ],
        "errors": [_api_error_payload(error) for error in state.errors],
        "run_id": state.run_id,
        "trace_id": state.trace_id,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 1 if state.status == "failed" else 0


def _missing_provider_config(provider: str, source: Mapping[str, str]) -> str | None:
    key = REAL_PROVIDER_REQUIREMENTS.get(provider)
    if key and not source.get(key):
        return f"missing {key}"
    if provider not in {"mock", *REAL_PROVIDER_REQUIREMENTS}:
        return "MULTIMODAL_AGENT_CHAT_PROVIDER must be mock, openai, qwen, or local."
    return None


def _api_error_payload(error: Any) -> dict[str, Any]:
    return api_error_from_agent_error(error).model_dump(mode="json")


def _print_provider_unconfigured(reason: str) -> None:
    print("provider_unconfigured")
    print(reason)
    print("Please set MULTIMODAL_AGENT_CHAT_PROVIDER and the required provider configuration.")


if __name__ == "__main__":
    raise SystemExit(main())
