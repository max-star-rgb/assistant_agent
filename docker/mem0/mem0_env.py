"""Environment resolution for the isolated Mem0 sidecar."""

from __future__ import annotations

import os
from collections.abc import Mapping


DEFAULT_QWEN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_EMBEDDING_MODEL = "text-embedding-v4"
CHINESE_MEMORY_CUSTOM_INSTRUCTIONS = """\
所有提取、合并或更新后的长期记忆文本必须使用自然、准确的简体中文。
即使原始对话包含英文，也要把可翻译的事实表达为简体中文；日期、金额、数字、URL、
型号和确有必要保留的专有名词或缩写必须保持准确。
这项要求只改变记忆文本的表达语言，不改变应当提取、合并、更新或忽略哪些事实。"""


def resolve_mem0_provider_environment(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Resolve sidecar settings from explicit overrides or repo dotenv names."""

    env = source if source is not None else os.environ
    qwen_api_key = _required_first(
        env,
        "OPENAI_API_KEY",
        "QWEN_API_KEY",
        "DASHSCOPE_API_KEY",
    )
    qwen_base_url = _first(
        env,
        "OPENAI_BASE_URL",
        "QWEN_CHAT_BASE_URL",
    ) or DEFAULT_QWEN_BASE_URL
    return {
        "chat_model": _required_first(
            env,
            "OPENAI_MODEL",
            "QWEN_CHAT_MODEL",
        ),
        "chat_api_key": qwen_api_key,
        "chat_base_url": qwen_base_url,
        "embedding_model": _first(
            env,
            "EMBEDDING_MODEL",
        ) or DEFAULT_EMBEDDING_MODEL,
        "embedding_api_key": _first(
            env,
            "EMBEDDING_API_KEY",
        ) or qwen_api_key,
        "embedding_base_url": _first(
            env,
            "EMBEDDING_BASE_URL",
        ) or qwen_base_url,
    }


def _required_first(source: Mapping[str, str], *names: str) -> str:
    value = _first(source, *names)
    if value:
        return value
    raise RuntimeError(
        "required sidecar environment variable is missing: " + " or ".join(names)
    )


def _first(source: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        value = source.get(name)
        if value and value.strip():
            return value.strip()
    return None
