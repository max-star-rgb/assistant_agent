"""Nested application configuration and section-local validation."""

from dataclasses import dataclass, field
from typing import Literal

from assistant_agent.provider_mode import ProviderMode
from assistant_agent.providers.specs import (
    ResolvedProviderSpec,
    resolved_chat_provider,
    resolved_image_generation_provider,
    resolved_vision_provider,
)


ContextCompactorMode = Literal["off", "llm"]
ConversationHistoryBackend = Literal["memory", "jsonl"]
LangGraphCheckpointerBackend = Literal["none", "memory", "sqlite"]
MemoryBackendName = Literal["disabled", "mem0", "langmem"]
VisionEmbeddingProviderName = Literal["mock", "dashscope", "local_siglip2"]
EmbeddingProviderName = Literal["mock", "dashscope", "local_siglip2"]
ShoppingSearchProviderName = Literal["mock", "http", "haodanku"]
ShoppingCompareProviderName = Literal["mock", "http", "haodanku"]
LodgingProviderName = Literal["mock", "flyai"]
VisualImageSearchProviderName = Literal["mock", "qwen"]
QwenChatApiProtocol = Literal["dashscope", "openai_compatible"]


@dataclass(frozen=True)
class RuntimeConfig:
    current_location: str | None = "上海市青浦区华为练秋湖研发中心"
    agent_service_text_turn_timeout_seconds: float = 90.0
    langgraph_checkpointer_backend: LangGraphCheckpointerBackend = "memory"
    langgraph_checkpoint_path: str | None = None


@dataclass(frozen=True)
class ChatConfig:
    openai_api_key: str | None = None
    qwen_api_key: str | None = None
    dashscope_api_key: str | None = None
    ark_api_key: str | None = None
    chat_provider: str = "mock"
    chat_api_key: str | None = None
    chat_base_url: str | None = None
    chat_model: str | None = None
    chat_stream: bool = False
    native_provider_streaming: bool = False
    chat_timeout_seconds: float = 75.0
    deep_research_chat_max_tokens: int = 8_192
    context_compactor_mode: ContextCompactorMode = "off"
    context_tokenizer_path: str | None = None
    context_input_token_limit: int = 128_000
    context_compaction_trigger_ratio: float = 0.75
    context_compaction_target_ratio: float = 0.15
    context_compaction_hard_ratio: float = 0.85
    context_compaction_safety_margin_tokens: int = 50_000
    context_summary_max_tokens: int = 32_768
    qwen_chat_enable_thinking: bool = False
    qwen_chat_enable_search: bool = False
    qwen_chat_api_protocol: QwenChatApiProtocol = "dashscope"
    openai_chat_base_url: str = "https://api.openai.com/v1"
    openai_chat_model: str = "gpt-4o-mini"
    qwen_chat_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_chat_workspace_id: str | None = None
    qwen_chat_model: str = "qwen-plus"
    deepseek_api_key: str | None = None
    deepseek_chat_base_url: str = "https://api.deepseek.com/v1"
    deepseek_chat_model: str = "deepseek-chat"
    ark_chat_api_key: str | None = None
    ark_chat_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    ark_chat_model: str | None = None
    local_chat_base_url: str | None = None
    local_chat_model: str = "local-chat"

    def __post_init__(self) -> None:
        if not (
            0.0
            < self.context_compaction_target_ratio
            < self.context_compaction_trigger_ratio
            < self.context_compaction_hard_ratio
            <= 1.0
        ):
            raise ValueError(
                "context compaction ratios must satisfy "
                "0 < target < trigger < hard <= 1"
            )

    def resolved_provider(self) -> ResolvedProviderSpec:
        return resolved_chat_provider(
            self.chat_provider,
            api_key=self.chat_api_key,
            base_url=self.chat_base_url,
            model=self.chat_model,
        )


@dataclass(frozen=True)
class VisionConfig:
    qwen_vision_api_key: str | None = None
    qwen_realtime_vision_api_key: str | None = None
    ark_vision_api_key: str | None = None
    seed_api_key: str | None = None
    vision_provider: str = "mock"
    vision_api_key: str | None = None
    vision_base_url: str | None = None
    vision_model: str | None = None
    vision_adapter_kind: str = "mock"
    vision_embedding_provider: VisionEmbeddingProviderName = "mock"
    embedding_provider: EmbeddingProviderName = "mock"
    vision_embedding_api_key: str | None = None
    vision_embedding_base_url: str = (
        "https://dashscope.aliyuncs.com/api/v1/services/embeddings/"
        "multimodal-embedding/multimodal-embedding"
    )
    vision_embedding_model: str = "tongyi-embedding-vision-flash-2026-03-06"
    vision_embedding_dimension: int = 768
    vision_embedding_timeout_seconds: float = 30.0
    siglip2_vision_model_dir: str | None = None
    siglip2_cuda_device_id: int = 0
    siglip2_model_dir: str | None = None
    embedding_cuda_device_id: int = 0
    keyframe_max_interval_seconds: float = 2.0
    keyframe_semantic_threshold: float = 0.08
    visual_memory_candidate_similarity: float = 0.20
    visual_memory_confirmed_similarity: float = 0.30
    visual_memory_qdrant_url: str = "http://127.0.0.1:6333"
    visual_memory_qdrant_collection: str = "assistant_visual_memory"
    visual_memory_qdrant_timeout_seconds: float = 2.0
    visual_memory_dense_model_cache_dir: str = ".data/models/fastembed"
    visual_memory_result_limit: int = 12
    visual_reminder_similarity_threshold: float = 0.82
    visual_reminder_max_active: int = 16
    visual_reminder_terminal_history_limit: int = 64
    openai_vision_base_url: str = "https://api.openai.com/v1"
    openai_vision_model: str = "gpt-4o-mini"
    qwen_vision_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_vision_model: str = "qwen-vl-plus"
    qwen_realtime_vision_base_url: str = (
        "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
    )
    qwen_realtime_vision_model: str = "qwen3.5-omni-flash-realtime"
    qwen_realtime_vision_workspace_id: str | None = None
    qwen_realtime_vision_region: str = "cn-beijing"
    ark_vision_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    ark_vision_model: str = "doubao-seed-2-0-lite-260215"
    seed_vision_base_url: str = "https://api.seed.example/v1/vision"
    seed_vision_model: str = "seed-vision"
    visual_context_compactor_mode: ContextCompactorMode = "off"
    visual_context_input_token_limit: int = 32_768
    visual_context_compaction_target_ratio: float = 0.40
    visual_context_compaction_trigger_ratio: float = 0.70
    visual_context_compaction_hard_ratio: float = 0.85
    visual_context_compaction_safety_margin_tokens: int = 2_048
    visual_context_summary_max_tokens: int = 2_048

    def __post_init__(self) -> None:
        if self.siglip2_cuda_device_id < 0:
            raise ValueError("siglip2 CUDA device id must be non-negative")
        if self.embedding_cuda_device_id < 0:
            raise ValueError("embedding CUDA device id must be non-negative")
        if self.keyframe_max_interval_seconds <= 0:
            raise ValueError("keyframe max interval must be positive")
        if not 0.0 <= self.keyframe_semantic_threshold <= 1.0:
            raise ValueError("keyframe semantic threshold must be between 0 and 1")
        if not (
            -1.0
            <= self.visual_memory_candidate_similarity
            < self.visual_memory_confirmed_similarity
            <= 1.0
        ):
            raise ValueError(
                "visual memory thresholds must satisfy candidate < confirmed"
            )
        if not self.visual_memory_qdrant_url.strip():
            raise ValueError("visual memory Qdrant URL must be non-empty")
        if not self.visual_memory_qdrant_collection.strip():
            raise ValueError("visual memory Qdrant collection must be non-empty")
        if self.visual_memory_qdrant_timeout_seconds <= 0:
            raise ValueError("visual memory Qdrant timeout must be positive")
        if not self.visual_memory_dense_model_cache_dir.strip():
            raise ValueError("visual memory dense model cache must be non-empty")
        if self.visual_memory_result_limit <= 0:
            raise ValueError("visual memory result limit must be positive")
        if not 0.0 <= self.visual_reminder_similarity_threshold <= 1.0:
            raise ValueError(
                "visual reminder similarity threshold must be within [0, 1]"
            )
        if self.visual_reminder_max_active <= 0:
            raise ValueError("visual reminder active limit must be positive")
        if self.visual_reminder_terminal_history_limit <= 0:
            raise ValueError("visual reminder terminal history limit must be positive")
        if not (
            0.0
            < self.visual_context_compaction_target_ratio
            < self.visual_context_compaction_trigger_ratio
            < self.visual_context_compaction_hard_ratio
            <= 1.0
        ):
            raise ValueError(
                "visual context compaction ratios must satisfy "
                "0 < target < trigger < hard <= 1"
            )

    def resolved_provider(self) -> ResolvedProviderSpec:
        return resolved_vision_provider(
            self.vision_provider,
            api_key=self.vision_api_key,
            base_url=self.vision_base_url,
            model=self.vision_model,
        )


@dataclass(frozen=True)
class MemoryConfig:
    mem0_base_url: str | None = None
    mem0_api_key: str | None = None
    mem0_timeout_seconds: float = 5.0
    mem0_identity_namespace: str = "assistant-agent"
    memory_backend: MemoryBackendName = "disabled"
    memory_commit_ledger_path: str = ".local/langgraph/memory_commits.sqlite3"
    memory_extraction_delay_seconds: int = 1_800
    langmem_model: str | None = None
    conversation_history_backend: ConversationHistoryBackend = "memory"
    conversation_history_path: str = ".local/memory/conversation_history.jsonl"
    max_conversation_history_turns: int = 0
    editable_context_enabled: bool = True
    editable_context_root: str = ".local/context"
    editable_context_user_id: str | None = None

    def __post_init__(self) -> None:
        if self.memory_backend not in {"disabled", "mem0", "langmem"}:
            raise ValueError("memory backend must be disabled, mem0, or langmem")
        if self.mem0_timeout_seconds <= 0:
            raise ValueError("Mem0 timeout must be positive")
        if not self.mem0_identity_namespace.strip():
            raise ValueError("Mem0 identity namespace must be non-empty")
        if not self.memory_commit_ledger_path.strip():
            raise ValueError("memory commit ledger path must be non-empty")
        if self.memory_extraction_delay_seconds <= 0:
            raise ValueError("memory extraction delay must be positive")


@dataclass(frozen=True)
class MediaConfig:
    proactive_message_delivery_timeout_seconds: float = 95.0
    proactive_delivery_store_path: str = ".local/agent_server/proactive_deliveries.sqlite3"
    proactive_delivery_ack_timeout_seconds: float = 15.0
    proactive_delivery_lease_seconds: float = 30.0
    proactive_delivery_presence_ttl_seconds: float = 45.0
    proactive_delivery_poll_interval_seconds: float = 0.25
    remote_visual_memory_enabled: bool = False
    remote_visual_memory_base_url: str | None = None
    remote_visual_memory_query_timeout_seconds: float = 5.0
    remote_visual_memory_query_top_k: int = 8
    remote_visual_memory_download_base_url: str | None = None
    remote_visual_memory_segment_seconds: float = 30.0
    remote_visual_memory_spool_root: str = ".data/remote_visual_memory"
    remote_visual_memory_file_ttl_seconds: int = 86_400
    remote_visual_memory_poll_interval_seconds: float = 2.0
    td_gen_ip: str | None = None
    td_gen_port: int | None = None
    public_ip: str | None = None
    public_port: int | None = None
    image_to_3d_timeout_seconds: float = 5.0
    video_understanding_timeout_seconds: float = 60.0
    max_video_bytes: int = 52_428_800
    max_video_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.proactive_message_delivery_timeout_seconds <= 0:
            raise ValueError("proactive message delivery timeout must be positive")
        if not self.proactive_delivery_store_path.strip():
            raise ValueError("proactive delivery store path must be non-empty")
        if self.proactive_delivery_ack_timeout_seconds <= 0:
            raise ValueError("proactive delivery ACK timeout must be positive")
        if self.proactive_delivery_lease_seconds <= 0:
            raise ValueError("proactive delivery lease must be positive")
        if self.proactive_delivery_presence_ttl_seconds <= 0:
            raise ValueError("proactive delivery presence TTL must be positive")
        if self.proactive_delivery_poll_interval_seconds <= 0:
            raise ValueError("proactive delivery poll interval must be positive")
        if self.remote_visual_memory_query_timeout_seconds <= 0:
            raise ValueError("remote visual memory query timeout must be positive")
        if self.remote_visual_memory_query_top_k <= 0:
            raise ValueError("remote visual memory top_k must be positive")
        if self.remote_visual_memory_segment_seconds <= 0:
            raise ValueError("remote visual memory segment duration must be positive")
        if not self.remote_visual_memory_spool_root.strip():
            raise ValueError("remote visual memory spool root must be non-empty")
        if self.remote_visual_memory_file_ttl_seconds <= 0:
            raise ValueError("remote visual memory file TTL must be positive")
        if self.remote_visual_memory_poll_interval_seconds <= 0:
            raise ValueError("remote visual memory poll interval must be positive")


@dataclass(frozen=True)
class ImageGenerationConfig:
    qwen_image_api_key: str | None = None
    ark_image_api_key: str | None = None
    comfyui_base_url: str | None = None
    image_generation_provider: str = "mock"
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

    def resolved_provider(self) -> ResolvedProviderSpec:
        return resolved_image_generation_provider(
            self.image_generation_provider,
            api_key=self.image_generation_api_key,
            base_url=self.image_generation_base_url,
            model=self.image_generation_model,
        )


@dataclass(frozen=True)
class SearchConfig:
    visual_image_search_provider: VisualImageSearchProviderName = "mock"
    qwen_image_search_api_key: str | None = None
    qwen_image_search_base_url: str = (
        "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )
    qwen_image_search_model: str = "qwen3.7-plus"
    qwen_image_search_timeout_seconds: float = 30.0


@dataclass(frozen=True)
class ShoppingConfig:
    shopping_search_provider: ShoppingSearchProviderName = "mock"
    shopping_search_base_url: str | None = None
    shopping_search_api_key: str | None = None
    shopping_search_timeout_seconds: float = 10.0
    shopping_compare_provider: ShoppingCompareProviderName = "mock"
    shopping_compare_base_url: str | None = None
    shopping_compare_api_key: str | None = None
    shopping_compare_timeout_seconds: float = 10.0
    haodanku_api_key: str | None = None
    haodanku_base_url: str = "https://v3.api.haodanku.com"
    haodanku_timeout_seconds: float = 10.0
    haodanku_enabled_platforms: tuple[str, ...] = ("taobao",)
    haodanku_taobao_pid: str | None = None
    haodanku_taobao_authorized_name: str | None = None
    haodanku_jd_sub_union_id: str | None = None
    haodanku_pdd_channel: str | None = None


@dataclass(frozen=True)
class LodgingConfig:
    lodging_provider: LodgingProviderName = "mock"
    flyai_cli_path: str | None = None
    flyai_api_key: str | None = None
    flyai_timeout_seconds: float = 30.0


@dataclass(frozen=True)
class ToolConfig:
    durable_tasks_enabled: bool = False
    image_generation: ImageGenerationConfig = field(
        default_factory=ImageGenerationConfig
    )
    search: SearchConfig = field(default_factory=SearchConfig)
    shopping: ShoppingConfig = field(default_factory=ShoppingConfig)
    lodging: LodgingConfig = field(default_factory=LodgingConfig)


@dataclass(frozen=True)
class AppConfig:
    provider_mode: ProviderMode = "mock"
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    chat: ChatConfig = field(default_factory=ChatConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    media: MediaConfig = field(default_factory=MediaConfig)
    tools: ToolConfig = field(default_factory=ToolConfig)

    def __post_init__(self) -> None:
        missing = self.chat.resolved_provider().missing_required_env()
        if self.provider_mode == "real" and (
            self.chat.chat_provider == "mock" or missing
        ):
            detail = f" Missing: {', '.join(missing)}." if missing else ""
            raise ValueError(
                "MULTIMODAL_AGENT_PROVIDER_MODE=real requires a non-mock "
                "MULTIMODAL_AGENT_CHAT_PROVIDER with complete configuration."
                f"{detail}"
            )
        if self.media.remote_visual_memory_enabled:
            if self.provider_mode != "real":
                raise ValueError("remote visual memory requires provider mode real")
            if self.memory.memory_backend != "langmem":
                raise ValueError("remote visual memory requires MEMORY_BACKEND=langmem")
            if not (self.media.remote_visual_memory_base_url or "").strip():
                raise ValueError("remote visual memory requires a base URL")

    def has_any_real_provider(self) -> bool:
        return any(
            (
                self.chat.openai_api_key,
                self.chat.qwen_api_key,
                self.chat.dashscope_api_key,
                self.chat.ark_api_key,
                self.vision.vision_embedding_api_key,
                self.vision.qwen_vision_api_key,
                self.vision.qwen_realtime_vision_api_key,
                self.tools.image_generation.qwen_image_api_key,
                self.vision.ark_vision_api_key,
                self.tools.image_generation.ark_image_api_key,
                self.chat.ark_chat_api_key,
                self.chat.deepseek_api_key,
                self.vision.seed_api_key,
                self.tools.image_generation.comfyui_base_url,
                self.tools.search.qwen_image_search_api_key,
                self.tools.shopping.shopping_search_api_key,
                self.tools.shopping.shopping_compare_api_key,
                self.tools.shopping.haodanku_api_key,
            )
        )
