"""Application and provider configuration."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from multimodal_agent.runtime_profile import RuntimeProfile
from multimodal_agent.schemas.provider_specs import (
    ResolvedProviderSpec,
    resolve_chat_provider,
    resolve_image_generation_provider,
    resolve_vision_provider,
    select_chat_provider,
    select_image_generation_provider,
    select_vision_provider,
)


AgentGraphMode = Literal["conditional", "assistant_loop"]
AssistantToolCallMode = Literal["auto", "prompt_json", "native_tools"]
ConversationHistoryBackend = Literal["memory", "jsonl"]
LangGraphCheckpointerBackend = Literal["none", "memory"]


VisionProviderName = str
ChatProviderName = str
ImageGenerationProviderName = str
ProductSearchProviderName = Literal["mock", "local_json", "http", "haodanku"]
PriceCompareProviderName = Literal["mock", "local", "http", "haodanku"]
RenderProviderName = Literal["mock", "http"]
VideoProviderName = Literal["mock", "http", "ark"]
IntentRouterName = Literal["rule", "mock_llm", "hybrid", "llm"]


@dataclass(frozen=True)
class ProviderConfig:
    """Provider settings loaded from environment variables.

    The config intentionally stores optional strings only. It does not validate
    real credentials or initialize provider clients.
    """

    runtime_profile: RuntimeProfile = RuntimeProfile.from_env({})
    openai_api_key: str | None = None
    qwen_api_key: str | None = None
    dashscope_api_key: str | None = None
    ark_api_key: str | None = None
    qwen_vision_api_key: str | None = None
    qwen_image_api_key: str | None = None
    ark_vision_api_key: str | None = None
    ark_image_api_key: str | None = None
    seed_api_key: str | None = None
    comfyui_base_url: str | None = None
    blender_render_url: str | None = None
    search_api_base_url: str | None = None
    vision_provider: VisionProviderName = "mock"
    vision_api_key: str | None = None
    vision_base_url: str | None = None
    vision_model: str | None = None
    vision_adapter_kind: str = "mock"
    openai_vision_base_url: str = "https://api.openai.com/v1"
    openai_vision_model: str = "gpt-4o-mini"
    qwen_vision_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_vision_model: str = "qwen-vl-plus"
    ark_vision_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    ark_vision_model: str = "doubao-seed-2-0-lite-260215"
    seed_vision_base_url: str = "https://api.seed.example/v1/vision"
    seed_vision_model: str = "seed-vision"
    memory_backend: Literal["memory", "jsonl"] = "memory"
    memory_path: str = ".local/memory/memories.jsonl"
    conversation_history_backend: ConversationHistoryBackend = "memory"
    conversation_history_path: str = ".local/memory/conversation_history.jsonl"
    max_conversation_history_turns: int = 8
    chat_provider: ChatProviderName = "mock"
    chat_api_key: str | None = None
    chat_base_url: str | None = None
    chat_model: str | None = None
    chat_adapter_kind: str = "mock"
    openai_chat_base_url: str = "https://api.openai.com/v1"
    openai_chat_model: str = "gpt-4o-mini"
    qwen_chat_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_chat_model: str = "qwen-plus"
    deepseek_api_key: str | None = None
    deepseek_chat_base_url: str = "https://api.deepseek.com/v1"
    deepseek_chat_model: str = "deepseek-chat"
    local_chat_base_url: str | None = None
    local_chat_model: str = "local-chat"
    image_generation_provider: ImageGenerationProviderName = "mock"
    image_generation_api_key: str | None = None
    image_generation_base_url: str | None = None
    image_generation_model: str | None = None
    image_generation_adapter_kind: str = "mock"
    openai_image_model: str = "gpt-image-1"
    qwen_image_base_url: str = "https://dashscope.aliyuncs.com/api/v1"
    qwen_image_model: str = "qwen-image-2.0-pro"
    qwen_image_default_size: str = "1024*1024"
    ark_image_base_url: str | None = None
    ark_image_model: str | None = None
    ark_image_default_size: str = "2K"
    ark_image_output_format: str = "png"
    local_image_base_url: str | None = None
    local_image_model: str = "local-image"
    product_search_provider: ProductSearchProviderName = "mock"
    product_search_local_path: str | None = None
    product_search_base_url: str | None = None
    product_search_api_key: str | None = None
    product_search_timeout_seconds: float = 10.0
    price_compare_provider: PriceCompareProviderName = "mock"
    price_compare_base_url: str | None = None
    price_compare_api_key: str | None = None
    price_compare_timeout_seconds: float = 10.0
    haodanku_api_key: str | None = None
    haodanku_base_url: str = "https://v3.api.haodanku.com"
    haodanku_timeout_seconds: float = 10.0
    render_provider: RenderProviderName = "mock"
    render_base_url: str | None = None
    render_api_key: str | None = None
    render_timeout_seconds: float = 10.0
    video_provider: VideoProviderName = "mock"
    video_understanding_base_url: str | None = None
    video_understanding_api_key: str | None = None
    video_understanding_model: str = "video-understanding"
    video_understanding_timeout_seconds: float = 60.0
    max_video_bytes: int = 52_428_800
    max_video_seconds: float = 60.0
    intent_router: IntentRouterName = "rule"
    agent_graph_mode: AgentGraphMode = "assistant_loop"  # 默认使用新的 ReAct 架构
    assistant_tool_call_mode: AssistantToolCallMode = "auto"
    langgraph_checkpointer_backend: LangGraphCheckpointerBackend = "memory"
    max_tool_iterations: int = 5
    max_plan_steps: int = 8
    max_plan_revisions: int = 2

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ProviderConfig":
        source = _clean_env_source(os.environ if env is None else env)
        if not source.get("DEEPSEEK_CHAT_API_KEY") and source.get("DEEPSEEK_API_KEY"):
            source["DEEPSEEK_CHAT_API_KEY"] = source["DEEPSEEK_API_KEY"]
        runtime_profile = RuntimeProfile.from_env(source)
        allow_real_providers = runtime_profile.allows_real_providers
        chat_provider = _chat_provider(
            source.get("MULTIMODAL_AGENT_CHAT_PROVIDER"),
            allow_real=allow_real_providers,
        )
        chat_settings = resolve_chat_provider(chat_provider, source)
        vision_provider = _vision_provider(
            source.get("MULTIMODAL_AGENT_VISION_PROVIDER"),
            allow_real=allow_real_providers,
        )
        vision_settings = resolve_vision_provider(vision_provider, source)
        image_generation_provider = _image_generation_provider(
            source.get("MULTIMODAL_AGENT_IMAGE_PROVIDER"),
            allow_real=allow_real_providers,
        )
        image_generation_settings = resolve_image_generation_provider(image_generation_provider, source)
        memory_backend = _memory_backend(source.get("MULTIMODAL_AGENT_MEMORY_BACKEND"))
        memory_path = source.get("MULTIMODAL_AGENT_MEMORY_PATH", ".local/memory/memories.jsonl")
        conversation_history_backend = _conversation_history_backend(
            source.get("MULTIMODAL_AGENT_CONVERSATION_HISTORY_BACKEND"),
            memory_backend=memory_backend,
        )
        conversation_history_path = source.get("MULTIMODAL_AGENT_CONVERSATION_HISTORY_PATH") or (
            _default_conversation_history_path(memory_path)
        )
        return cls(
            runtime_profile=runtime_profile,
            openai_api_key=source.get("OPENAI_API_KEY"),
            qwen_api_key=source.get("QWEN_API_KEY"),
            dashscope_api_key=source.get("DASHSCOPE_API_KEY"),
            ark_api_key=source.get("ARK_API_KEY"),
            qwen_vision_api_key=source.get("QWEN_VISION_API_KEY"),
            qwen_image_api_key=source.get("QWEN_IMAGE_API_KEY"),
            ark_vision_api_key=source.get("ARK_VISION_API_KEY"),
            ark_image_api_key=source.get("ARK_IMAGE_API_KEY"),
            seed_api_key=source.get("SEED_API_KEY"),
            deepseek_api_key=source.get("DEEPSEEK_CHAT_API_KEY") or source.get("DEEPSEEK_API_KEY"),
            comfyui_base_url=source.get("COMFYUI_BASE_URL"),
            blender_render_url=source.get("BLENDER_RENDER_URL"),
            search_api_base_url=source.get("SEARCH_API_BASE_URL"),
            vision_provider=vision_provider,
            vision_api_key=vision_settings.api_key,
            vision_base_url=vision_settings.base_url,
            vision_model=vision_settings.model,
            vision_adapter_kind=vision_settings.spec.adapter_kind,
            openai_vision_base_url=source.get("OPENAI_VISION_BASE_URL", "https://api.openai.com/v1"),
            openai_vision_model=source.get("OPENAI_VISION_MODEL", "gpt-4o-mini"),
            qwen_vision_base_url=source.get(
                "QWEN_VISION_BASE_URL",
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
            ),
            qwen_vision_model=source.get("QWEN_VISION_MODEL", "qwen-vl-plus"),
            ark_vision_base_url=source.get("ARK_VISION_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"),
            ark_vision_model=source.get("ARK_VISION_MODEL", "doubao-seed-2-0-lite-260215"),
            seed_vision_base_url=source.get("SEED_VISION_BASE_URL", "https://api.seed.example/v1/vision"),
            seed_vision_model=source.get("SEED_VISION_MODEL", "seed-vision"),
            memory_backend=memory_backend,
            memory_path=memory_path,
            conversation_history_backend=conversation_history_backend,
            conversation_history_path=conversation_history_path,
            max_conversation_history_turns=_int_env(
                source.get("MULTIMODAL_AGENT_MAX_CONVERSATION_HISTORY_TURNS")
                or source.get("MULTIMODAL_AGENT_MAX_CONVERSATION_TURNS"),
                8,
            ),
            chat_provider=chat_provider,
            chat_api_key=chat_settings.api_key,
            chat_base_url=chat_settings.base_url,
            chat_model=chat_settings.model,
            chat_adapter_kind=chat_settings.spec.adapter_kind,
            openai_chat_base_url=source.get("OPENAI_CHAT_BASE_URL", "https://api.openai.com/v1"),
            openai_chat_model=source.get("OPENAI_CHAT_MODEL", "gpt-4o-mini"),
            qwen_chat_base_url=source.get("QWEN_CHAT_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            qwen_chat_model=source.get("QWEN_CHAT_MODEL", "qwen-plus"),
            deepseek_chat_base_url=source.get("DEEPSEEK_CHAT_BASE_URL", "https://api.deepseek.com/v1"),
            deepseek_chat_model=source.get("DEEPSEEK_CHAT_MODEL", "deepseek-chat"),
            local_chat_base_url=source.get("LOCAL_CHAT_BASE_URL"),
            local_chat_model=source.get("LOCAL_CHAT_MODEL", "local-chat"),
            image_generation_provider=image_generation_provider,
            image_generation_api_key=image_generation_settings.api_key,
            image_generation_base_url=image_generation_settings.base_url,
            image_generation_model=image_generation_settings.model,
            image_generation_adapter_kind=image_generation_settings.spec.adapter_kind,
            openai_image_model=source.get("OPENAI_IMAGE_MODEL", "gpt-image-1"),
            qwen_image_base_url=source.get("QWEN_IMAGE_BASE_URL", "https://dashscope.aliyuncs.com/api/v1"),
            qwen_image_model=source.get("QWEN_IMAGE_MODEL", "qwen-image-2.0-pro"),
            qwen_image_default_size="1024*1024",
            ark_image_base_url=source.get("ARK_IMAGE_BASE_URL"),
            ark_image_model=source.get("ARK_IMAGE_MODEL"),
            ark_image_default_size="2K",
            ark_image_output_format="png",
            local_image_base_url=source.get("LOCAL_IMAGE_BASE_URL"),
            local_image_model=source.get("LOCAL_IMAGE_MODEL", "local-image"),
            product_search_provider=_product_search_provider(
                source.get("MULTIMODAL_AGENT_PRODUCT_PROVIDER"),
                allow_real=allow_real_providers,
            ),
            product_search_local_path=source.get("PRODUCT_SEARCH_LOCAL_PATH"),
            product_search_base_url=source.get("PRODUCT_SEARCH_BASE_URL") or source.get("SEARCH_API_BASE_URL"),
            product_search_api_key=source.get("PRODUCT_SEARCH_API_KEY"),
            product_search_timeout_seconds=_float_env(source.get("PRODUCT_SEARCH_TIMEOUT_SECONDS"), 10.0),
            price_compare_provider=_price_compare_provider(
                source.get("MULTIMODAL_AGENT_PRICE_PROVIDER"),
                allow_real=allow_real_providers,
            ),
            price_compare_base_url=source.get("PRICE_COMPARE_BASE_URL"),
            price_compare_api_key=source.get("PRICE_COMPARE_API_KEY"),
            price_compare_timeout_seconds=_float_env(source.get("PRICE_COMPARE_TIMEOUT_SECONDS"), 10.0),
            haodanku_api_key=source.get("HAODANKU_API_KEY"),
            haodanku_base_url=source.get("HAODANKU_BASE_URL") or "https://v3.api.haodanku.com",
            haodanku_timeout_seconds=_float_env(source.get("HAODANKU_TIMEOUT_SECONDS"), 10.0),
            render_provider=_render_provider(
                source.get("MULTIMODAL_AGENT_RENDER_PROVIDER"),
                allow_real=allow_real_providers,
            ),
            render_base_url=source.get("RENDER_BASE_URL") or source.get("BLENDER_RENDER_URL"),
            render_api_key=source.get("RENDER_API_KEY"),
            render_timeout_seconds=_float_env(source.get("RENDER_TIMEOUT_SECONDS"), 10.0),
            video_provider=_video_provider(
                source.get("MULTIMODAL_AGENT_VIDEO_PROVIDER"),
                allow_real=allow_real_providers,
            ),
            video_understanding_base_url=_video_base_url(source),
            video_understanding_api_key=_video_api_key(source),
            video_understanding_model=_video_model(source),
            video_understanding_timeout_seconds=_float_env(
                source.get("VIDEO_UNDERSTANDING_TIMEOUT_SECONDS"),
                60.0,
            ),
            max_video_bytes=_int_env(source.get("MULTIMODAL_AGENT_MAX_VIDEO_BYTES"), 52_428_800),
            max_video_seconds=_float_env(source.get("MULTIMODAL_AGENT_MAX_VIDEO_SECONDS"), 60.0),
            intent_router=_intent_router(source.get("MULTIMODAL_AGENT_INTENT_ROUTER")),
            agent_graph_mode=_agent_graph_mode(source.get("AGENT_GRAPH_MODE")),
            assistant_tool_call_mode=_assistant_tool_call_mode(
                source.get("ASSISTANT_TOOL_CALL_MODE")
                or source.get("MULTIMODAL_AGENT_ASSISTANT_TOOL_CALL_MODE")
            ),
            langgraph_checkpointer_backend=_langgraph_checkpointer_backend(
                source.get("LANGGRAPH_CHECKPOINTER_BACKEND")
                or source.get("MULTIMODAL_AGENT_CHECKPOINTER_BACKEND")
            ),
            max_tool_iterations=_int_env(source.get("MAX_TOOL_ITERATIONS"), 5),
            max_plan_steps=_int_env(source.get("MAX_PLAN_STEPS"), 8),
            max_plan_revisions=_int_env(source.get("MAX_PLAN_REVISIONS"), 2),
        )

    def has_any_real_provider(self) -> bool:
        return any(
            (
                self.openai_api_key,
                self.qwen_api_key,
                self.dashscope_api_key,
                self.ark_api_key,
                self.qwen_vision_api_key,
                self.qwen_image_api_key,
                self.ark_vision_api_key,
                self.ark_image_api_key,
                self.deepseek_api_key,
                self.seed_api_key,
                self.comfyui_base_url,
                self.blender_render_url,
                self.search_api_base_url,
                self.product_search_api_key,
                self.price_compare_api_key,
                self.haodanku_api_key,
                self.render_api_key,
                self.video_understanding_api_key,
            )
        )

    def resolved_chat_provider(self) -> ResolvedProviderSpec:
        """Return selected chat provider config, including legacy field compatibility."""

        if self.chat_api_key or self.chat_base_url or self.chat_model:
            return resolve_chat_provider(
                self.chat_provider,
                {
                    "OPENAI_API_KEY": self.chat_api_key or "",
                    "OPENAI_CHAT_BASE_URL": self.chat_base_url or "",
                    "OPENAI_CHAT_MODEL": self.chat_model or "",
                    "QWEN_API_KEY": self.chat_api_key or "",
                    "QWEN_CHAT_BASE_URL": self.chat_base_url or "",
                    "QWEN_CHAT_MODEL": self.chat_model or "",
                    "DEEPSEEK_CHAT_API_KEY": self.chat_api_key or "",
                    "DEEPSEEK_API_KEY": self.chat_api_key or "",
                    "DEEPSEEK_CHAT_BASE_URL": self.chat_base_url or "",
                    "DEEPSEEK_CHAT_MODEL": self.chat_model or "",
                    "LOCAL_CHAT_BASE_URL": self.chat_base_url or "",
                    "LOCAL_CHAT_MODEL": self.chat_model or "",
                },
            )
        return resolve_chat_provider(
            self.chat_provider,
            {
                "OPENAI_API_KEY": self.openai_api_key or "",
                "OPENAI_CHAT_BASE_URL": self.openai_chat_base_url,
                "OPENAI_CHAT_MODEL": self.openai_chat_model,
                "QWEN_API_KEY": self.qwen_api_key or "",
                "QWEN_CHAT_BASE_URL": self.qwen_chat_base_url,
                "QWEN_CHAT_MODEL": self.qwen_chat_model,
                "DEEPSEEK_CHAT_API_KEY": self.deepseek_api_key or "",
                "DEEPSEEK_API_KEY": self.deepseek_api_key or "",
                "DEEPSEEK_CHAT_BASE_URL": self.deepseek_chat_base_url,
                "DEEPSEEK_CHAT_MODEL": self.deepseek_chat_model,
                "LOCAL_CHAT_BASE_URL": self.local_chat_base_url or "",
                "LOCAL_CHAT_MODEL": self.local_chat_model,
            },
        )

    def resolved_vision_provider(self) -> ResolvedProviderSpec:
        """Return selected Vision provider config, including legacy field compatibility."""

        if self.vision_api_key or self.vision_base_url or self.vision_model:
            return resolve_vision_provider(
                self.vision_provider,
                {
                    "OPENAI_API_KEY": self.vision_api_key or "",
                    "OPENAI_VISION_BASE_URL": self.vision_base_url or "",
                    "OPENAI_VISION_MODEL": self.vision_model or "",
                    "QWEN_API_KEY": self.vision_api_key or "",
                    "QWEN_VISION_API_KEY": self.vision_api_key or "",
                    "QWEN_VISION_BASE_URL": self.vision_base_url or "",
                    "QWEN_VISION_MODEL": self.vision_model or "",
                    "ARK_API_KEY": self.vision_api_key or "",
                    "ARK_VISION_API_KEY": self.vision_api_key or "",
                    "ARK_VISION_BASE_URL": self.vision_base_url or "",
                    "ARK_VISION_MODEL": self.vision_model or "",
                    "SEED_API_KEY": self.vision_api_key or "",
                    "SEED_VISION_BASE_URL": self.vision_base_url or "",
                    "SEED_VISION_MODEL": self.vision_model or "",
                },
            )
        return resolve_vision_provider(
            self.vision_provider,
            {
                "OPENAI_API_KEY": self.openai_api_key or "",
                "OPENAI_VISION_BASE_URL": self.openai_vision_base_url,
                "OPENAI_VISION_MODEL": self.openai_vision_model,
                "QWEN_API_KEY": self.qwen_api_key or "",
                "QWEN_VISION_API_KEY": self.qwen_vision_api_key or self.qwen_api_key or "",
                "QWEN_VISION_BASE_URL": self.qwen_vision_base_url,
                "QWEN_VISION_MODEL": self.qwen_vision_model,
                "ARK_API_KEY": self.ark_api_key or "",
                "ARK_VISION_API_KEY": self.ark_vision_api_key or self.ark_api_key or "",
                "ARK_VISION_BASE_URL": self.ark_vision_base_url,
                "ARK_VISION_MODEL": self.ark_vision_model,
                "SEED_API_KEY": self.seed_api_key or "",
                "SEED_VISION_BASE_URL": self.seed_vision_base_url,
                "SEED_VISION_MODEL": self.seed_vision_model,
            },
        )

    def resolved_image_generation_provider(self) -> ResolvedProviderSpec:
        """Return selected image generation provider config with legacy compatibility."""

        if self.image_generation_api_key or self.image_generation_base_url or self.image_generation_model:
            return resolve_image_generation_provider(
                self.image_generation_provider,
                {
                    "OPENAI_API_KEY": self.image_generation_api_key or "",
                    "OPENAI_IMAGE_MODEL": self.image_generation_model or "",
                    "DASHSCOPE_API_KEY": self.image_generation_api_key or "",
                    "QWEN_IMAGE_API_KEY": self.image_generation_api_key or "",
                    "QWEN_IMAGE_BASE_URL": self.image_generation_base_url or "",
                    "QWEN_IMAGE_MODEL": self.image_generation_model or "",
                    "ARK_API_KEY": self.image_generation_api_key or "",
                    "ARK_IMAGE_API_KEY": self.image_generation_api_key or "",
                    "ARK_IMAGE_BASE_URL": self.image_generation_base_url or "",
                    "ARK_IMAGE_MODEL": self.image_generation_model or "",
                    "COMFYUI_BASE_URL": self.image_generation_base_url or "",
                    "LOCAL_IMAGE_BASE_URL": self.image_generation_base_url or "",
                    "LOCAL_IMAGE_MODEL": self.image_generation_model or "",
                },
            )
        return resolve_image_generation_provider(
            self.image_generation_provider,
            {
                "OPENAI_API_KEY": self.openai_api_key or "",
                "OPENAI_IMAGE_MODEL": self.openai_image_model,
                "DASHSCOPE_API_KEY": self.dashscope_api_key or "",
                "QWEN_IMAGE_API_KEY": self.qwen_image_api_key or self.dashscope_api_key or "",
                "QWEN_IMAGE_BASE_URL": self.qwen_image_base_url,
                "QWEN_IMAGE_MODEL": self.qwen_image_model,
                "ARK_API_KEY": self.ark_api_key or "",
                "ARK_IMAGE_API_KEY": self.ark_image_api_key or self.ark_api_key or "",
                "ARK_IMAGE_BASE_URL": self.ark_image_base_url,
                "ARK_IMAGE_MODEL": self.ark_image_model,
                "COMFYUI_BASE_URL": self.comfyui_base_url or "",
                "LOCAL_IMAGE_BASE_URL": self.local_image_base_url or "",
                "LOCAL_IMAGE_MODEL": self.local_image_model,
            },
        )


def _memory_backend(value: str | None) -> Literal["memory", "jsonl"]:
    if value == "jsonl":
        return "jsonl"
    return "memory"


def _conversation_history_backend(
    value: str | None,
    *,
    memory_backend: Literal["memory", "jsonl"],
) -> ConversationHistoryBackend:
    if value == "jsonl":
        return "jsonl"
    if value == "memory":
        return "memory"
    return "jsonl" if memory_backend == "jsonl" else "memory"


def _langgraph_checkpointer_backend(value: str | None) -> LangGraphCheckpointerBackend:
    if value == "none":
        return "none"
    return "memory"


def _default_conversation_history_path(memory_path: str) -> str:
    return str(Path(memory_path).with_name("conversation_history.jsonl"))


def _vision_provider(value: str | None, *, allow_real: bool = True) -> VisionProviderName:
    return select_vision_provider(value, allow_real=allow_real)


def _chat_provider(value: str | None, *, allow_real: bool = True) -> ChatProviderName:
    return select_chat_provider(value, allow_real=allow_real)


def _image_generation_provider(value: str | None, *, allow_real: bool = True) -> ImageGenerationProviderName:
    return select_image_generation_provider(value, allow_real=allow_real)


def _product_search_provider(value: str | None, *, allow_real: bool = True) -> ProductSearchProviderName:
    if value == "local_json":
        return "local_json"
    if not allow_real:
        return "mock"
    if value in {"local_json", "http", "haodanku"}:
        return value
    return "mock"


def _price_compare_provider(value: str | None, *, allow_real: bool = True) -> PriceCompareProviderName:
    if value == "local":
        return "local"
    if not allow_real:
        return "mock"
    if value in {"http", "haodanku"}:
        return value
    return "mock"


def _render_provider(value: str | None, *, allow_real: bool = True) -> RenderProviderName:
    if allow_real and value == "http":
        return "http"
    return "mock"


def _video_provider(value: str | None, *, allow_real: bool = True) -> VideoProviderName:
    if allow_real and value == "http":
        return "http"
    if allow_real and value == "ark":
        return "ark"
    return "mock"


def _clean_env_source(source: Mapping[str, str]) -> dict[str, str]:
    return {key: _clean_env_value(value) for key, value in source.items()}


def _clean_env_value(value: str) -> str:
    cleaned = value.strip()
    if " #" in cleaned:
        cleaned = cleaned.split(" #", 1)[0].strip()
    if len(cleaned) >= 2 and (cleaned[0], cleaned[-1]) in {('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’")}:
        cleaned = cleaned[1:-1]
    return cleaned.strip().strip('"').strip("'").strip("“”‘’")


def _video_base_url(source: Mapping[str, str]) -> str | None:
    if source.get("MULTIMODAL_AGENT_VIDEO_PROVIDER") == "ark":
        return source.get("ARK_VISION_BASE_URL") or "https://ark.cn-beijing.volces.com/api/v3"
    return source.get("VIDEO_UNDERSTANDING_BASE_URL")


def _video_api_key(source: Mapping[str, str]) -> str | None:
    if source.get("MULTIMODAL_AGENT_VIDEO_PROVIDER") == "ark":
        return source.get("ARK_VISION_API_KEY")
    return source.get("VIDEO_UNDERSTANDING_API_KEY")


def _video_model(source: Mapping[str, str]) -> str:
    if source.get("MULTIMODAL_AGENT_VIDEO_PROVIDER") == "ark":
        return source.get("ARK_VISION_MODEL") or "doubao-seed-2-0-lite-260215"
    return source.get("VIDEO_UNDERSTANDING_MODEL", "video-understanding")


def _intent_router(value: str | None) -> IntentRouterName:
    if value in {"mock_llm", "hybrid", "llm"}:
        return value
    return "rule"


def _agent_graph_mode(value: str | None) -> AgentGraphMode:
    if value == "conditional":
        return "conditional"
    return "assistant_loop"  # 默认改为 assistant_loop


def _assistant_tool_call_mode(value: str | None) -> AssistantToolCallMode:
    if value == "native_tools":
        return "native_tools"
    if value == "prompt_json":
        return "prompt_json"
    if value == "auto":
        return "auto"
    return "auto"


def _float_env(value: str | None, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _int_env(value: str | None, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def should_run_integration_tests(env: Mapping[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    return source.get("RUN_INTEGRATION_TESTS") == "1"
