#!/usr/bin/env python3
"""Smoke native tool-calling flow without requiring real providers by default."""

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
from assistant_agent.schemas.assistant_decision import NativeToolCall
from assistant_agent.schemas.api import api_error_from_agent_error
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.chat_adapter import ChatRequest, ChatResult
from assistant_agent.services.provider_specs import resolve_chat_provider, supported_chat_providers


class ScriptedNativeToolChatAdapter:
    """Small local adapter that behaves like an OpenAI-compatible native tool caller."""

    provider = "scripted-native"
    model = "native-tool-smoke"

    def __init__(self, query: str, final_answer: str) -> None:
        self.query = query
        self.final_answer = final_answer
        self.requests: list[ChatRequest] = []
        self.results: list[ChatResult] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        if len(self.requests) == 1:
            result = ChatResult(
                response_text="",
                tool_calls=[
                    NativeToolCall(
                        id="native_call_1",
                        name="shopping_search",
                        arguments={"query": self.query, "limit": 2},
                        raw={
                            "id": "native_call_1",
                            "type": "function",
                            "function": {"name": "shopping_search", "arguments": "{}"},
                        },
                    )
                ],
                finish_reason="tool_calls",
                message_kind="tool_call",
                provider=self.provider,
                model=self.model,
            )
        else:
            result = ChatResult(
                response_text=self.final_answer,
                finish_reason="stop",
                message_kind="final_answer",
                provider=self.provider,
                model=self.model,
            )
        self.results.append(result)
        return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Smoke the ReAct native tool-calling path. Defaults to a local scripted "
            "adapter; use --real-provider only for explicit provider smoke."
        )
    )
    parser.add_argument("--query", default="帮我找一款通勤蓝牙耳机", help="User query for the smoke run.")
    parser.add_argument("--user-id", default="native_smoke_user", help="User id for this smoke run.")
    parser.add_argument("--session-id", default="native_smoke_session", help="Session id for this smoke run.")
    parser.add_argument(
        "--real-provider",
        action="store_true",
        help="Use configured real chat provider. Requires provider_smoke env and credentials.",
    )
    parser.add_argument(
        "--expect-tool",
        default="shopping_search",
        help="Tool expected in the run. Use empty string to disable this assertion.",
    )
    return parser


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source = dict(env if env is not None else os.environ)
    scripted_adapter: ScriptedNativeToolChatAdapter | None = None

    if args.real_provider:
        missing = _missing_real_provider_config(source)
        if missing:
            _print_provider_unconfigured(missing)
            return 2
        config = ProviderConfig.from_env(source)
        runtime = AgentGraphRuntime(config=config)
        provider = config.chat_provider
        model = config.chat_model
    else:
        config = ProviderConfig.from_env({})
        scripted_adapter = ScriptedNativeToolChatAdapter(
            query=args.query,
            final_answer="已通过 native tool calling 搜索商品，并基于 observation 生成最终回答。",
        )
        runtime = AgentGraphRuntime(config=config, chat_adapter=scripted_adapter)
        provider = scripted_adapter.provider
        model = scripted_adapter.model

    request = UserRequest(user_id=args.user_id, session_id=args.session_id, text=args.query)
    state = runtime.run_state(request)
    output = _smoke_payload(
        state,
        provider=provider,
        model=model,
        scripted_adapter=scripted_adapter,
        expected_tool=args.expect_tool.strip() or None,
    )
    print(json.dumps(output, ensure_ascii=False, indent=2))
    if state.status == "failed" or output["expectation_failed"]:
        return 1
    return 0


def _missing_real_provider_config(source: Mapping[str, str]) -> str | None:
    if source.get("MULTIMODAL_AGENT_RUNTIME_PROFILE") != "provider_smoke":
        return "missing MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke"
    provider = source.get("MULTIMODAL_AGENT_CHAT_PROVIDER", "mock")
    if provider == "mock":
        return "missing non-mock MULTIMODAL_AGENT_CHAT_PROVIDER"
    if provider not in supported_chat_providers():
        return f"MULTIMODAL_AGENT_CHAT_PROVIDER must be one of: {', '.join(supported_chat_providers())}."
    missing = resolve_chat_provider(provider, source).missing_required_env()
    if missing:
        return f"missing {', '.join(missing)}"
    return None


def _smoke_payload(
    state: Any,
    *,
    provider: str,
    model: str | None,
    scripted_adapter: ScriptedNativeToolChatAdapter | None,
    expected_tool: str | None,
) -> dict[str, Any]:
    tool_sequence = [call.tool_name for call in state.tool_calls]
    native_tool_calls = state.request.metadata.get("native_tool_calls", [])
    react_steps = state.request.metadata.get("assistant_loop_steps", [])
    expected_tool_seen = expected_tool is None or expected_tool in tool_sequence
    return {
        "status": "success" if state.status != "failed" and expected_tool_seen else "failed",
        "provider": provider,
        "model": model,
        "native_tool_calls": native_tool_calls if isinstance(native_tool_calls, list) else [],
        "provider_decisions": _scripted_decisions(scripted_adapter),
        "tool_sequence": tool_sequence,
        "expected_tool": expected_tool,
        "expectation_failed": not expected_tool_seen,
        "react_steps": react_steps if isinstance(react_steps, list) else [],
        "response_text": state.response.message if state.response else "",
        "errors": [_api_error_payload(error) for error in state.errors],
        "run_id": state.run_id,
        "trace_id": state.trace_id,
    }


def _scripted_decisions(adapter: ScriptedNativeToolChatAdapter | None) -> list[dict[str, Any]]:
    if adapter is None:
        return []
    return [
        {
            "finish_reason": result.finish_reason,
            "message_kind": result.message_kind,
            "tool_calls": [call.name for call in result.tool_calls],
            "response_text_present": bool(result.response_text),
        }
        for result in adapter.results
    ]


def _api_error_payload(error: Any) -> dict[str, Any]:
    return api_error_from_agent_error(error).model_dump(mode="json")


def _print_provider_unconfigured(reason: str) -> None:
    print("provider_unconfigured")
    print(reason)
    print(
        "Set MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke, "
        "MULTIMODAL_AGENT_CHAT_PROVIDER, and the provider credentials before using --real-provider."
    )


if __name__ == "__main__":
    raise SystemExit(main())
