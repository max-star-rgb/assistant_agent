"""Application and provider configuration."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal


VisionProviderName = Literal["mock", "openai", "qwen", "seed"]
ChatProviderName = Literal["mock", "openai", "qwen", "local"]
ImageGenerationProviderName = Literal["mock", "openai", "qwen", "comfyui", "local"]
ProductSearchProviderName = Literal["mock", "local_json", "http"]
PriceCompareProviderName = Literal["mock", "local", "http"]
RenderProviderName = Literal["mock", "http"]
VideoProviderName = Literal["mock", "http"]
IntentRouterName = Literal["rule", "mock_llm", "hybrid", "llm"]


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
    chat_provider: ChatProviderName = "mock"
    openai_chat_base_url: str = "https://api.openai.com/v1"
    openai_chat_model: str = "gpt-4o-mini"
    qwen_chat_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_chat_model: str = "qwen-plus"
    local_chat_base_url: str | None = None
    local_chat_model: str = "local-chat"
    image_generation_provider: ImageGenerationProviderName = "mock"
    openai_image_model: str = "gpt-image-1"
    qwen_image_model: str = "wanx2.1-t2i-turbo"
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
            chat_provider=_chat_provider(source.get("MULTIMODAL_AGENT_CHAT_PROVIDER")),
            openai_chat_base_url=source.get("OPENAI_CHAT_BASE_URL", "https://api.openai.com/v1"),
            openai_chat_model=source.get("OPENAI_CHAT_MODEL", "gpt-4o-mini"),
            qwen_chat_base_url=source.get("QWEN_CHAT_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            qwen_chat_model=source.get("QWEN_CHAT_MODEL", "qwen-plus"),
            local_chat_base_url=source.get("LOCAL_CHAT_BASE_URL"),
            local_chat_model=source.get("LOCAL_CHAT_MODEL", "local-chat"),
            image_generation_provider=_image_generation_provider(source.get("MULTIMODAL_AGENT_IMAGE_PROVIDER")),
            openai_image_model=source.get("OPENAI_IMAGE_MODEL", "gpt-image-1"),
            qwen_image_model=source.get("QWEN_IMAGE_MODEL", "wanx2.1-t2i-turbo"),
            local_image_base_url=source.get("LOCAL_IMAGE_BASE_URL"),
            local_image_model=source.get("LOCAL_IMAGE_MODEL", "local-image"),
            product_search_provider=_product_search_provider(source.get("MULTIMODAL_AGENT_PRODUCT_PROVIDER")),
            product_search_local_path=source.get("PRODUCT_SEARCH_LOCAL_PATH"),
            product_search_base_url=source.get("PRODUCT_SEARCH_BASE_URL") or source.get("SEARCH_API_BASE_URL"),
            product_search_api_key=source.get("PRODUCT_SEARCH_API_KEY"),
            product_search_timeout_seconds=_float_env(source.get("PRODUCT_SEARCH_TIMEOUT_SECONDS"), 10.0),
            price_compare_provider=_price_compare_provider(source.get("MULTIMODAL_AGENT_PRICE_PROVIDER")),
            price_compare_base_url=source.get("PRICE_COMPARE_BASE_URL"),
            price_compare_api_key=source.get("PRICE_COMPARE_API_KEY"),
            price_compare_timeout_seconds=_float_env(source.get("PRICE_COMPARE_TIMEOUT_SECONDS"), 10.0),
            render_provider=_render_provider(source.get("MULTIMODAL_AGENT_RENDER_PROVIDER")),
            render_base_url=source.get("RENDER_BASE_URL") or source.get("BLENDER_RENDER_URL"),
            render_api_key=source.get("RENDER_API_KEY"),
            render_timeout_seconds=_float_env(source.get("RENDER_TIMEOUT_SECONDS"), 10.0),
            video_provider=_video_provider(source.get("MULTIMODAL_AGENT_VIDEO_PROVIDER")),
            video_understanding_base_url=source.get("VIDEO_UNDERSTANDING_BASE_URL"),
            video_understanding_api_key=source.get("VIDEO_UNDERSTANDING_API_KEY"),
            video_understanding_model=source.get("VIDEO_UNDERSTANDING_MODEL", "video-understanding"),
            video_understanding_timeout_seconds=_float_env(
                source.get("VIDEO_UNDERSTANDING_TIMEOUT_SECONDS"),
                60.0,
            ),
            max_video_bytes=_int_env(source.get("MULTIMODAL_AGENT_MAX_VIDEO_BYTES"), 52_428_800),
            max_video_seconds=_float_env(source.get("MULTIMODAL_AGENT_MAX_VIDEO_SECONDS"), 60.0),
            intent_router=_intent_router(source.get("MULTIMODAL_AGENT_INTENT_ROUTER")),
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
                self.product_search_api_key,
                self.price_compare_api_key,
                self.render_api_key,
                self.video_understanding_api_key,
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


def _chat_provider(value: str | None) -> ChatProviderName:
    if value in {"openai", "qwen", "local"}:
        return value
    return "mock"


def _image_generation_provider(value: str | None) -> ImageGenerationProviderName:
    if value in {"openai", "qwen", "comfyui", "local"}:
        return value
    return "mock"


def _product_search_provider(value: str | None) -> ProductSearchProviderName:
    if value in {"local_json", "http"}:
        return value
    return "mock"


def _price_compare_provider(value: str | None) -> PriceCompareProviderName:
    if value in {"local", "http"}:
        return value
    return "mock"


def _render_provider(value: str | None) -> RenderProviderName:
    if value == "http":
        return "http"
    return "mock"


def _video_provider(value: str | None) -> VideoProviderName:
    if value == "http":
        return "http"
    return "mock"


def _intent_router(value: str | None) -> IntentRouterName:
    if value in {"mock_llm", "hybrid", "llm"}:
        return value
    return "rule"


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
