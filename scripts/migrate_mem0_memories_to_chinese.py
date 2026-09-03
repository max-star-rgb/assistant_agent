#!/usr/bin/env python3
"""Inspect or migrate one runtime user's Mem0 memories to Simplified Chinese."""

# ruff: noqa: E402

from __future__ import annotations

import json
import sys
from argparse import ArgumentParser
from dataclasses import replace
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from assistant_agent.config import ChatConfig, load_app_config
from assistant_agent.identity import DEFAULT_AGENT_ID, RequestIdentity
from assistant_agent.memory.mem0.chinese_migration import (
    migrate_memories_to_chinese,
)
from assistant_agent.memory.mem0.transport import (
    Mem0HttpRequest,
    urllib_mem0_transport,
)
from assistant_agent.config_env import load_env_file
from assistant_agent.runtime.chat_adapter import (
    ChatAdapter,
    ChatRequest,
    OpenAICompatibleChatAdapter,
)


def migration_apply_gate_error(
    *,
    provider_mode: str,
    chat_provider: str,
    mem0_base_url: str | None,
    allow_real_provider: bool,
) -> str | None:
    """Return the first stable apply-gate failure, if any."""

    if provider_mode != "real":
        return "real_provider_mode_required"
    if chat_provider != "qwen":
        return "qwen_provider_required"
    if not mem0_base_url:
        return "mem0_base_url_required"
    if not allow_real_provider:
        return "operator_confirmation_required"
    return None


def translate_memory_to_chinese(
    adapter: ChatAdapter,
    text: str,
) -> str:
    """Translate one memory through the configured governed chat adapter."""

    result = adapter.chat(
        ChatRequest(
            user_id="memory-language-migration",
            session_id="memory-language-migration",
            user_query=text,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你只负责把一条长期记忆翻译成自然、准确的简体中文。"
                        "逐句直译，只输出译文，不解释、不概括、不添加或删除事实。"
                        "必须保留原文出现的每个日期、金额、数字、百分比、URL、"
                        "型号和必要的专有名词或缩写；即使某个事实能由其他信息推导，"
                        "也不得省略。"
                    ),
                },
                {"role": "user", "content": text},
            ],
            tools=[],
            tool_choice="none",
            temperature=0.0,
            max_tokens=1024,
        )
    )
    if result.errors or not result.response_text.strip():
        raise RuntimeError("qwen memory translation failed")
    return result.response_text.strip()


def create_memory_translation_adapter(
    config: ChatConfig,
) -> OpenAICompatibleChatAdapter:
    """Create the configured Qwen adapter without native web search."""

    settings = config.resolved_provider()
    if (
        settings.provider != "qwen"
        or settings.adapter_kind != "openai_compatible"
    ):
        raise ValueError("memory translation requires Qwen")
    return OpenAICompatibleChatAdapter(
        provider=settings.provider,
        api_key=settings.api_key or "",
        base_url=settings.base_url or "",
        model=settings.model or "",
        timeout_seconds=config.chat_timeout_seconds,
        stream=config.chat_stream,
        enable_thinking=config.qwen_chat_enable_thinking,
        native_web_search=False,
    )


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(
        description="Inspect or migrate one user's Mem0 memories to Chinese."
    )
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--agent-id", default=DEFAULT_AGENT_ID)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--no-env-file", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--allow-real-provider", action="store_true")
    args = parser.parse_args(argv)

    if not args.no_env_file:
        load_env_file(args.env_file)
    loaded_config = load_app_config()
    provider_mode = loaded_config.provider_mode
    chat_config = loaded_config.chat
    memory_config = loaded_config.memory
    del loaded_config
    if not memory_config.mem0_base_url:
        return _print_error("mem0_base_url_required")

    if args.apply:
        gate_error = migration_apply_gate_error(
            provider_mode=provider_mode,
            chat_provider=chat_config.chat_provider,
            mem0_base_url=memory_config.mem0_base_url,
            allow_real_provider=args.allow_real_provider,
        )
        if gate_error:
            return _print_error(gate_error)
        provider = chat_config.resolved_provider()
        missing = provider.missing_required_env()
        if missing:
            return _print_error(
                "qwen_provider_not_configured",
                details={"missing": sorted(missing)},
            )
        adapter: ChatAdapter | None = create_memory_translation_adapter(chat_config)
    else:
        adapter = None

    base_transport = urllib_mem0_transport(memory_config.mem0_base_url)
    headers = (
        {"X-API-Key": memory_config.mem0_api_key}
        if memory_config.mem0_api_key
        else None
    )

    def transport(request: Mem0HttpRequest):
        return base_transport(
            replace(request, headers=headers or request.headers)
        )

    try:
        report = migrate_memories_to_chinese(
            identity=RequestIdentity.for_user(
                user_id=args.user_id,
                agent_id=args.agent_id,
                session_id="memory-language-migration",
            ),
            identity_namespace=memory_config.mem0_identity_namespace,
            transport=transport,
            translate=(
                (lambda text: translate_memory_to_chinese(adapter, text))
                if adapter is not None
                else _translation_must_not_run
            ),
            apply=args.apply,
            timeout_seconds=memory_config.mem0_timeout_seconds,
        )
    except Exception:
        return _print_error("memory_migration_request_failed")

    payload = {
        "mode": "apply" if args.apply else "inspect",
        "user_id": args.user_id,
        "agent_id": args.agent_id,
        **report.model_dump(mode="json"),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if report.error_code is None else 1


def _translation_must_not_run(_: str) -> str:
    raise RuntimeError("translation is disabled in inspect mode")


def _print_error(
    code: str,
    *,
    details: dict[str, object] | None = None,
) -> Literal[2]:
    payload: dict[str, object] = {"error": code}
    if details:
        payload["details"] = details
    print(json.dumps(payload, ensure_ascii=False))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
