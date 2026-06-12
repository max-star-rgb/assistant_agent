"""Application and provider configuration."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal


VisionProviderName = Literal["mock", "openai", "qwen", "seed"]


@dataclass(frozen=True)
class ProviderConfig:
    """Provider settings loaded from environment variables.

    The config intentionally stores optional strings only. It does not validate
    real credentials or initialize provider clients.
    """

    openai_api_key: str | None = None
    qwen_api_key: str | None = None
    seed_api_key: str | None = None
    comfyui_base_url: str | None = None
    blender_render_url: str | None = None
    search_api_base_url: str | None = None
    vision_provider: VisionProviderName = "mock"
    openai_vision_base_url: str = "https://api.openai.com/v1"
    openai_vision_model: str = "gpt-4o-mini"
    qwen_vision_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_vision_model: str = "qwen-vl-plus"
    seed_vision_base_url: str = "https://api.seed.example/v1/vision"
    seed_vision_model: str = "seed-vision"
    memory_backend: Literal["memory", "jsonl"] = "memory"
    memory_path: str = ".local/memory/memories.jsonl"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ProviderConfig":
        source = os.environ if env is None else env
        return cls(
            openai_api_key=source.get("OPENAI_API_KEY"),
            qwen_api_key=source.get("QWEN_API_KEY"),
            seed_api_key=source.get("SEED_API_KEY"),
            comfyui_base_url=source.get("COMFYUI_BASE_URL"),
            blender_render_url=source.get("BLENDER_RENDER_URL"),
            search_api_base_url=source.get("SEARCH_API_BASE_URL"),
            vision_provider=_vision_provider(source.get("MULTIMODAL_AGENT_VISION_PROVIDER")),
            openai_vision_base_url=source.get("OPENAI_VISION_BASE_URL", "https://api.openai.com/v1"),
            openai_vision_model=source.get("OPENAI_VISION_MODEL", "gpt-4o-mini"),
            qwen_vision_base_url=source.get(
                "QWEN_VISION_BASE_URL",
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
            ),
            qwen_vision_model=source.get("QWEN_VISION_MODEL", "qwen-vl-plus"),
            seed_vision_base_url=source.get("SEED_VISION_BASE_URL", "https://api.seed.example/v1/vision"),
            seed_vision_model=source.get("SEED_VISION_MODEL", "seed-vision"),
            memory_backend=_memory_backend(source.get("MULTIMODAL_AGENT_MEMORY_BACKEND")),
            memory_path=source.get("MULTIMODAL_AGENT_MEMORY_PATH", ".local/memory/memories.jsonl"),
        )

    def has_any_real_provider(self) -> bool:
        return any(
            (
                self.openai_api_key,
                self.qwen_api_key,
                self.seed_api_key,
                self.comfyui_base_url,
                self.blender_render_url,
                self.search_api_base_url,
            )
        )


def _memory_backend(value: str | None) -> Literal["memory", "jsonl"]:
    if value == "jsonl":
        return "jsonl"
    return "memory"


def _vision_provider(value: str | None) -> VisionProviderName:
    if value in {"openai", "qwen", "seed"}:
        return value
    return "mock"


def should_run_integration_tests(env: Mapping[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    return source.get("RUN_INTEGRATION_TESTS") == "1"
