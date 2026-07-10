"""Manual smoke entry point for direct_chat text capability."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.api import api_error_from_agent_error
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.event_sink import ListEventSink
from assistant_agent.services.provider_specs import resolve_chat_provider, supported_chat_providers


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
    source = dict(env if env is not None else os.environ)
    provider = source.get("MULTIMODAL_AGENT_CHAT_PROVIDER", "mock")

    missing = _missing_provider_config(provider, source)
    if missing:
        _print_provider_unconfigured(missing)
        return 2
    invalid_environment = _invalid_provider_smoke_environment(provider, source)
    if invalid_environment:
        _print_environment_invalid(invalid_environment)
        return 2

    config = ProviderConfig.from_env(source)
    request = UserRequest(
        user_id=args.user_id,
        session_id=args.session_id,
        text=args.text,
    )
    event_sink = ListEventSink()
    state = AgentGraphRuntime(config=config).run_state(request, event_sink=event_sink)

    output = {
        "status": "success" if state.status != "failed" else "failed",
        "provider": provider,
        "capability": "direct_chat",
        "runtime_profile": config.runtime_profile.name,
        "native_provider_streaming": config.native_provider_streaming,
        "native_runtime": _used_native_runtime(state),
        "intent": state.intent.intent if state.intent else None,
        "response_text": state.response.message if state.response else "",
        "response_delta_text": _response_delta_text(event_sink),
        "event_counts": _event_counts(event_sink),
        "provider_budget": state.provider_budget.summary(),
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
    if provider not in supported_chat_providers():
        return f"MULTIMODAL_AGENT_CHAT_PROVIDER must be one of: {', '.join(supported_chat_providers())}."
    missing = resolve_chat_provider(provider, source).missing_required_env()
    if missing:
        return f"missing {', '.join(missing)}"
    return None


def _invalid_provider_smoke_environment(provider: str, source: Mapping[str, str]) -> str | None:
    if provider == "mock":
        return None
    return _invalid_proxy_environment(source)


def _invalid_proxy_environment(source: Mapping[str, str]) -> str | None:
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        value = source.get(name)
        if not value:
            continue
        scheme = urlparse(value).scheme.lower()
        if not scheme:
            return f"{name} proxy URL is missing a scheme; use http:// or https://, or unset {name}."
        if scheme == "socks":
            return (
                f"{name} has unsupported proxy URL scheme 'socks'; "
                f"use http:// or https:// for this smoke, or unset {name}."
            )
    return None


def _api_error_payload(error: Any) -> dict[str, Any]:
    return api_error_from_agent_error(error).model_dump(mode="json")


def _used_native_runtime(state: Any) -> bool:
    response_data = state.response.data if state.response is not None and state.response.data else {}
    return bool(state.request.metadata.get("native_runtime") or response_data.get("native_runtime"))


def _response_delta_text(event_sink: ListEventSink) -> str:
    return "".join(event.text or "" for event in event_sink.events if event.type == "response_delta")


def _event_counts(event_sink: ListEventSink) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in event_sink.events:
        counts[event.type] = counts.get(event.type, 0) + 1
    return dict(sorted(counts.items()))


def _print_provider_unconfigured(reason: str) -> None:
    print("provider_unconfigured")
    print(reason)
    print("Please set MULTIMODAL_AGENT_CHAT_PROVIDER and the required provider configuration.")


def _print_environment_invalid(reason: str) -> None:
    print("environment_invalid")
    print(reason)
    print("Please fix the local environment before running provider smoke.")


if __name__ == "__main__":
    raise SystemExit(main())
