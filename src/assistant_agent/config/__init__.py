"""Application and provider configuration."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from assistant_agent.provider_mode import ProviderMode, get_provider_mode
from assistant_agent.providers.specs import (
    ResolvedProviderSpec,
    resolve_chat_provider,
    resolve_image_generation_provider,
    resolve_vision_provider,
    select_chat_provider,
    select_image_generation_provider,
    select_vision_provider,
)


AgentGraphMode = Literal["conditional", "assistant_loop"]
ContextCompactorMode = Literal["off", "deterministic", "llm"]
ConversationHistoryBackend = Literal["memory", "jsonl"]
LangGraphCheckpointerBackend = Literal["none", "memory"]


DEFAULT_QWEN_REALTIME_VISION_BASE_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
DEFAULT_QWEN_REALTIME_VISION_REGION = "cn-beijing"
DEFAULT_QWEN_IMAGE_SEARCH_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_QWEN_IMAGE_SEARCH_MODEL = "qwen3.7-plus"
SUPPORTED_QWEN_REALTIME_VISION_REGIONS = {"cn-beijing", "ap-southeast-1"}


VisionProviderName = str
VisionEmbeddingProviderName = Literal["mock", "dashscope"]
ChatProviderName = str
ImageGenerationProviderName = str
ShoppingSearchProviderName = Literal["mock", "local_json", "http", "haodanku"]
ShoppingCompareProviderName = Literal["mock", "local", "http", "haodanku"]
IntentRouterName = Literal["rule", "mock_llm", "hybrid", "llm"]
SearchProviderName = Literal["mock", "http", "tavily"]
VisualImageSearchProviderName = Literal["mock", "qwen"]


@dataclass(frozen=True)
class ProviderConfig:
    """Provider settings loaded from environment variables.

    The config intentionally stores optional strings only. It does not validate
    real credentials or initialize provider clients.
    """

    provider_mode: ProviderMode = "mock"
    openai_api_key: str | None = None
    qwen_api_key: str | None = None
    dashscope_api_key: str | None = None
    ark_api_key: str | None = None
    qwen_vision_api_key: str | None = None
    qwen_realtime_vision_api_key: str | None = None
    qwen_image_api_key: str | None = None
    ark_vision_api_key: str | None = None
    ark_image_api_key: str | None = None
    seed_api_key: str | None = None
    comfyui_base_url: str | None = None
    search_api_base_url: str | None = None
    vision_provider: VisionProviderName = "mock"
    vision_api_key: str | None = None
    vision_base_url: str | None = None
    vision_model: str | None = None
    vision_adapter_kind: str = "mock"
    vision_embedding_provider: VisionEmbeddingProviderName = "mock"
    vision_embedding_api_key: str | None = None
    vision_embedding_base_url: str = (
        "https://dashscope.aliyuncs.com/api/v1/services/embeddings/"
        "multimodal-embedding/multimodal-embedding"
    )
    vision_embedding_model: str = "tongyi-embedding-vision-flash-2026-03-06"
    vision_embedding_dimension: int = 768
    vision_embedding_timeout_seconds: float = 30.0
    openai_vision_base_url: str = "https://api.openai.com/v1"
    openai_vision_model: str = "gpt-4o-mini"
    qwen_vision_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_vision_model: str = "qwen-vl-plus"
    qwen_realtime_vision_base_url: str = DEFAULT_QWEN_REALTIME_VISION_BASE_URL
    qwen_realtime_vision_model: str = "qwen3.5-omni-flash-realtime"
    qwen_realtime_vision_workspace_id: str | None = None
    qwen_realtime_vision_region: str = DEFAULT_QWEN_REALTIME_VISION_REGION
    ark_vision_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    ark_vision_model: str = "doubao-seed-2-0-lite-260215"
    seed_vision_base_url: str = "https://api.seed.example/v1/vision"
    seed_vision_model: str = "seed-vision"
    mem0_base_url: str | None = None
    mem0_api_key: str | None = None
    mem0_timeout_seconds: float = 5.0
    mem0_identity_namespace: str = "assistant-agent"
    memory_ingestion_max_workers: int = 2
    memory_ingestion_max_pending: int = 64
    memory_ingestion_shutdown_timeout_seconds: float = 10.0
    memory_session_snapshot_max_entries: int = 1024
    conversation_history_backend: ConversationHistoryBackend = "memory"
    conversation_history_path: str = ".local/memory/conversation_history.jsonl"
    max_conversation_history_turns: int = 0
    editable_context_enabled: bool = False
    editable_context_root: str = ".local/context"
    editable_context_user_id: str | None = None
    local_file_access_root: str = ".data/files"
    chat_provider: ChatProviderName = "mock"
    chat_api_key: str | None = None
    chat_base_url: str | None = None
    chat_model: str | None = None
    chat_adapter_kind: str = "mock"
    chat_stream: bool = False
    native_provider_streaming: bool = False
    chat_timeout_seconds: float = 75.0
    agent_service_text_turn_timeout_seconds: float = 90.0
    context_compactor_mode: ContextCompactorMode = "off"
    qwen_chat_enable_thinking: bool = False
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
    search_provider: SearchProviderName = "mock"
    web_search_base_url: str | None = None
    web_search_api_key: str | None = None
    web_search_timeout_seconds: float = 10.0
    tavily_api_key: str | None = None
    tavily_base_url: str = "https://api.tavily.com"
    visual_image_search_provider: VisualImageSearchProviderName = "mock"
    qwen_image_search_api_key: str | None = None
    qwen_image_search_base_url: str = DEFAULT_QWEN_IMAGE_SEARCH_BASE_URL
    qwen_image_search_model: str = DEFAULT_QWEN_IMAGE_SEARCH_MODEL
    qwen_image_search_timeout_seconds: float = 30.0
    shopping_search_provider: ShoppingSearchProviderName = "mock"
    shopping_search_local_path: str | None = None
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
    video_understanding_timeout_seconds: float = 60.0
    max_video_bytes: int = 52_428_800
    max_video_seconds: float = 60.0
    intent_router: IntentRouterName = "rule"
    agent_graph_mode: AgentGraphMode = "assistant_loop"  # 默认使用新的 ReAct 架构
    langgraph_checkpointer_backend: LangGraphCheckpointerBackend = "memory"
    max_tool_iterations: int = 5
    max_plan_steps: int = 8
    max_plan_revisions: int = 2
    durable_tasks_enabled: bool = False
    durable_task_path: str = ".local/tasks/durable_tasks.sqlite3"
    durable_notification_path: str = ".local/tasks/notifications.sqlite3"
    durable_task_worker_enabled: bool = False
    durable_notification_worker_enabled: bool = False
    durable_task_lease_seconds: int = 30
    durable_task_poll_seconds: float = 1.0
    durable_task_max_seconds: int = 2_592_000
    durable_workflow_max_quanta: int = 1_000

    def __post_init__(self) -> None:
        self.validate_provider_mode()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ProviderConfig":
        source = _clean_env_source(os.environ if env is None else env)
        if not source.get("QWEN_API_KEY") and source.get("DASHSCOPE_API_KEY"):
            source["QWEN_API_KEY"] = source["DASHSCOPE_API_KEY"]
        qwen_chat_workspace_id = _qwen_chat_workspace_id(source)
        source["QWEN_CHAT_BASE_URL"] = _qwen_chat_base_url(
            source,
            workspace_id=qwen_chat_workspace_id,
        )
        provider_mode = get_provider_mode(source)
        allow_real_providers = provider_mode == "real"
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
        vision_embedding_provider = _vision_embedding_provider(
            source.get("MULTIMODAL_AGENT_VISION_EMBEDDING_PROVIDER"),
            allow_real=allow_real_providers,
        )
        image_generation_provider = _image_generation_provider(
            source.get("MULTIMODAL_AGENT_IMAGE_PROVIDER"),
            allow_real=allow_real_providers,
        )
        image_generation_settings = resolve_image_generation_provider(image_generation_provider, source)
        qwen_realtime_vision_workspace_id = _qwen_realtime_vision_workspace_id(source)
        qwen_realtime_vision_region = _qwen_realtime_vision_region(
            source.get("QWEN_REALTIME_VISION_REGION")
        )
        conversation_history_backend = _conversation_history_backend(
            source.get("MULTIMODAL_AGENT_CONVERSATION_HISTORY_BACKEND"),
        )
        conversation_history_path = source.get("MULTIMODAL_AGENT_CONVERSATION_HISTORY_PATH") or (
            ".local/memory/conversation_history.jsonl"
        )
        config = cls(
            provider_mode=provider_mode,
            openai_api_key=source.get("OPENAI_API_KEY"),
            qwen_api_key=_qwen_provider_api_key(source),
            dashscope_api_key=source.get("DASHSCOPE_API_KEY"),
            ark_api_key=_ark_provider_api_key(source),
            qwen_vision_api_key=_qwen_capability_api_key(source, "QWEN_VISION_API_KEY"),
            qwen_realtime_vision_api_key=_qwen_capability_api_key(source, "QWEN_VISION_API_KEY"),
            qwen_image_api_key=_qwen_capability_api_key(source, "QWEN_IMAGE_API_KEY"),
            ark_vision_api_key=_ark_capability_api_key(source, "ARK_VISION_API_KEY"),
            ark_image_api_key=_ark_capability_api_key(source, "ARK_IMAGE_API_KEY"),
            ark_chat_api_key=_ark_capability_api_key(source, "ARK_CHAT_API_KEY"),
            seed_api_key=source.get("SEED_API_KEY"),
            deepseek_api_key=_deepseek_provider_api_key(source),
            comfyui_base_url=source.get("COMFYUI_BASE_URL"),
            search_api_base_url=source.get("SEARCH_API_BASE_URL"),
            vision_provider=vision_provider,
            vision_api_key=vision_settings.api_key,
            vision_base_url=vision_settings.base_url,
            vision_model=vision_settings.model,
            vision_adapter_kind=vision_settings.adapter_kind,
            vision_embedding_provider=vision_embedding_provider,
            vision_embedding_api_key=(
                _vision_embedding_api_key(source) if vision_embedding_provider == "dashscope" else None
            ),
            vision_embedding_base_url=source.get(
                "DASHSCOPE_MULTIMODAL_EMBEDDING_BASE_URL",
                "https://dashscope.aliyuncs.com/api/v1/services/embeddings/"
                "multimodal-embedding/multimodal-embedding",
            ),
            vision_embedding_model=source.get(
                "DASHSCOPE_VISION_EMBEDDING_MODEL",
                "tongyi-embedding-vision-flash-2026-03-06",
            ),
            vision_embedding_dimension=_int_env(source.get("DASHSCOPE_VISION_EMBEDDING_DIMENSION"), 768),
            vision_embedding_timeout_seconds=_float_env(
                source.get("DASHSCOPE_VISION_EMBEDDING_TIMEOUT_SECONDS"),
                30.0,
            ),
            openai_vision_base_url=source.get("OPENAI_VISION_BASE_URL", "https://api.openai.com/v1"),
            openai_vision_model=source.get("OPENAI_VISION_MODEL", "gpt-4o-mini"),
            qwen_vision_base_url=source.get(
                "QWEN_VISION_BASE_URL",
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
            ),
            qwen_vision_model=source.get("QWEN_VISION_MODEL", "qwen-vl-plus"),
            qwen_realtime_vision_base_url=_qwen_realtime_vision_base_url(
                source,
                workspace_id=qwen_realtime_vision_workspace_id,
                region=qwen_realtime_vision_region,
            ),
            qwen_realtime_vision_model=source.get(
                "QWEN_REALTIME_VISION_MODEL",
                "qwen3.5-omni-flash-realtime",
            ),
            qwen_realtime_vision_workspace_id=qwen_realtime_vision_workspace_id,
            qwen_realtime_vision_region=qwen_realtime_vision_region,
            ark_vision_base_url=source.get("ARK_VISION_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"),
            ark_vision_model=source.get("ARK_VISION_MODEL", "doubao-seed-2-0-lite-260215"),
            seed_vision_base_url=source.get("SEED_VISION_BASE_URL", "https://api.seed.example/v1/vision"),
            seed_vision_model=source.get("SEED_VISION_MODEL", "seed-vision"),
            mem0_base_url=source.get("MEM0_BASE_URL"),
            mem0_api_key=source.get("MEM0_API_KEY"),
            mem0_timeout_seconds=_float_env(
                source.get("MEM0_TIMEOUT_SECONDS"),
                5.0,
            ),
            mem0_identity_namespace=(
                source.get("MEM0_IDENTITY_NAMESPACE")
                or "assistant-agent"
            ),
            memory_ingestion_max_workers=max(
                1,
                _int_env(source.get("MULTIMODAL_AGENT_MEMORY_INGESTION_MAX_WORKERS"), 2),
            ),
            memory_ingestion_max_pending=max(
                1,
                _int_env(source.get("MULTIMODAL_AGENT_MEMORY_INGESTION_MAX_PENDING"), 64),
            ),
            memory_ingestion_shutdown_timeout_seconds=max(
                0.0,
                _float_env(
                    source.get("MULTIMODAL_AGENT_MEMORY_INGESTION_SHUTDOWN_TIMEOUT_SECONDS"),
                    10.0,
                ),
            ),
            memory_session_snapshot_max_entries=max(
                1,
                _int_env(
                    source.get("MULTIMODAL_AGENT_MEMORY_SESSION_SNAPSHOT_MAX_ENTRIES"),
                    1024,
                ),
            ),
            conversation_history_backend=conversation_history_backend,
            conversation_history_path=conversation_history_path,
            max_conversation_history_turns=_int_env(
                source.get("MULTIMODAL_AGENT_MAX_CONVERSATION_HISTORY_TURNS")
                or source.get("MULTIMODAL_AGENT_MAX_CONVERSATION_TURNS"),
                0,
            ),
            editable_context_enabled=_bool_env(
                source.get("MULTIMODAL_AGENT_EDITABLE_CONTEXT_ENABLED"),
                False,
            ),
            editable_context_root=(
                source.get("MULTIMODAL_AGENT_EDITABLE_CONTEXT_ROOT") or ".local/context"
            ),
            editable_context_user_id=(
                source.get("MULTIMODAL_AGENT_EDITABLE_CONTEXT_USER_ID") or None
            ),
            local_file_access_root=(
                source.get("MULTIMODAL_AGENT_FILE_ACCESS_ROOT") or ".data/files"
            ),
            chat_provider=chat_provider,
            chat_api_key=chat_settings.api_key,
            chat_base_url=chat_settings.base_url,
            chat_model=chat_settings.model,
            chat_adapter_kind=chat_settings.adapter_kind,
            chat_stream=_chat_stream(source, chat_provider),
            native_provider_streaming=_bool_env(
                source.get("MULTIMODAL_AGENT_NATIVE_PROVIDER_STREAMING"),
                chat_provider == "qwen" and _chat_stream(source, chat_provider),
            ),
            chat_timeout_seconds=_float_env(
                source.get("MULTIMODAL_AGENT_CHAT_TIMEOUT_SECONDS"),
                75.0,
            ),
            agent_service_text_turn_timeout_seconds=_float_env(
                source.get("ASSISTANT_AGENT_TEXT_TURN_TIMEOUT_SECONDS"),
                90.0,
            ),
            context_compactor_mode=_context_compactor_mode(
                source.get("MULTIMODAL_AGENT_CONTEXT_COMPACTOR")
            ),
            qwen_chat_enable_thinking=_bool_env(
                source.get("QWEN_CHAT_ENABLE_THINKING"),
                False,
            ),
            durable_tasks_enabled=_bool_env(
                source.get("MULTIMODAL_AGENT_DURABLE_TASKS_ENABLED"),
                False,
            ),
            durable_task_path=(
                source.get("MULTIMODAL_AGENT_DURABLE_TASK_PATH")
                or ".local/tasks/durable_tasks.sqlite3"
            ),
            durable_notification_path=(
                source.get("MULTIMODAL_AGENT_DURABLE_NOTIFICATION_PATH")
                or ".local/tasks/notifications.sqlite3"
            ),
            durable_task_worker_enabled=_bool_env(
                source.get("MULTIMODAL_AGENT_DURABLE_TASK_WORKER_ENABLED"),
                False,
            ),
            durable_notification_worker_enabled=_bool_env(
                source.get(
                    "MULTIMODAL_AGENT_DURABLE_NOTIFICATION_WORKER_ENABLED"
                ),
                False,
            ),
            durable_task_lease_seconds=max(
                5,
                _int_env(source.get("MULTIMODAL_AGENT_DURABLE_TASK_LEASE_SECONDS"), 30),
            ),
            durable_task_poll_seconds=max(
                0.1,
                _float_env(source.get("MULTIMODAL_AGENT_DURABLE_TASK_POLL_SECONDS"), 1.0),
            ),
            durable_task_max_seconds=max(
                3_600,
                _int_env(
                    source.get("MULTIMODAL_AGENT_DURABLE_TASK_MAX_SECONDS"),
                    2_592_000,
                ),
            ),
            durable_workflow_max_quanta=max(
                1,
                _int_env(
                    source.get("MULTIMODAL_AGENT_DURABLE_WORKFLOW_MAX_QUANTA"),
                    1_000,
                ),
            ),
            openai_chat_base_url=source.get("OPENAI_CHAT_BASE_URL", "https://api.openai.com/v1"),
            openai_chat_model=source.get("OPENAI_CHAT_MODEL", "gpt-4o-mini"),
            qwen_chat_base_url=source.get("QWEN_CHAT_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            qwen_chat_workspace_id=qwen_chat_workspace_id,
            qwen_chat_model=source.get("QWEN_CHAT_MODEL", "qwen-plus"),
            deepseek_chat_base_url=source.get("DEEPSEEK_CHAT_BASE_URL", "https://api.deepseek.com/v1"),
            deepseek_chat_model=source.get("DEEPSEEK_CHAT_MODEL", "deepseek-chat"),
            ark_chat_base_url=source.get("ARK_CHAT_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"),
            ark_chat_model=source.get("ARK_CHAT_MODEL"),
            local_chat_base_url=source.get("LOCAL_CHAT_BASE_URL"),
            local_chat_model=source.get("LOCAL_CHAT_MODEL", "local-chat"),
            image_generation_provider=image_generation_provider,
            image_generation_api_key=image_generation_settings.api_key,
            image_generation_base_url=image_generation_settings.base_url,
            image_generation_model=image_generation_settings.model,
            image_generation_adapter_kind=image_generation_settings.adapter_kind,
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
            search_provider=_search_provider(
                source.get("MULTIMODAL_AGENT_SEARCH_PROVIDER"),
                allow_real=allow_real_providers,
            ),
            web_search_base_url=source.get("WEB_SEARCH_BASE_URL"),
            web_search_api_key=source.get("WEB_SEARCH_API_KEY"),
            web_search_timeout_seconds=_float_env(source.get("WEB_SEARCH_TIMEOUT_SECONDS"), 10.0),
            tavily_api_key=source.get("TAVILY_API_KEY"),
            tavily_base_url=source.get("TAVILY_BASE_URL", "https://api.tavily.com"),
            visual_image_search_provider=_visual_image_search_provider(
                source.get("MULTIMODAL_AGENT_VISUAL_IMAGE_SEARCH_PROVIDER"),
                allow_real=allow_real_providers,
            ),
            qwen_image_search_api_key=(
                _qwen_capability_api_key(source, "QWEN_IMAGE_SEARCH_API_KEY")
            ),
            qwen_image_search_base_url=source.get(
                "QWEN_IMAGE_SEARCH_BASE_URL",
                DEFAULT_QWEN_IMAGE_SEARCH_BASE_URL,
            ),
            qwen_image_search_model=source.get(
                "QWEN_IMAGE_SEARCH_MODEL",
                DEFAULT_QWEN_IMAGE_SEARCH_MODEL,
            ),
            qwen_image_search_timeout_seconds=_float_env(
                source.get("QWEN_IMAGE_SEARCH_TIMEOUT_SECONDS"),
                30.0,
            ),
            shopping_search_provider=_shopping_search_provider(
                source.get("MULTIMODAL_AGENT_SHOPPING_PROVIDER"),
                allow_real=allow_real_providers,
            ),
            shopping_search_local_path=source.get("SHOPPING_SEARCH_LOCAL_PATH"),
            shopping_search_base_url=(
                source.get("SHOPPING_SEARCH_BASE_URL")
                or source.get("SEARCH_API_BASE_URL")
            ),
            shopping_search_api_key=source.get("SHOPPING_SEARCH_API_KEY"),
            shopping_search_timeout_seconds=_float_env(
                source.get("SHOPPING_SEARCH_TIMEOUT_SECONDS"),
                10.0,
            ),
            shopping_compare_provider=_shopping_compare_provider(
                source.get("MULTIMODAL_AGENT_SHOPPING_PROVIDER"),
                allow_real=allow_real_providers,
            ),
            shopping_compare_base_url=source.get("SHOPPING_COMPARE_BASE_URL"),
            shopping_compare_api_key=source.get("SHOPPING_COMPARE_API_KEY"),
            shopping_compare_timeout_seconds=_float_env(
                source.get("SHOPPING_COMPARE_TIMEOUT_SECONDS"),
                10.0,
            ),
            haodanku_api_key=source.get("HAODANKU_API_KEY"),
            haodanku_base_url=source.get("HAODANKU_BASE_URL") or "https://v3.api.haodanku.com",
            haodanku_timeout_seconds=_float_env(source.get("HAODANKU_TIMEOUT_SECONDS"), 10.0),
            haodanku_enabled_platforms=_haodanku_enabled_platforms(
                source.get("HAODANKU_ENABLED_PLATFORMS")
            ),
            haodanku_taobao_pid=source.get("HAODANKU_TAOBAO_PID"),
            haodanku_taobao_authorized_name=source.get("HAODANKU_TAOBAO_AUTHORIZED_NAME"),
            haodanku_jd_sub_union_id=source.get("HAODANKU_JD_SUB_UNION_ID"),
            haodanku_pdd_channel=source.get("HAODANKU_PDD_CHANNEL"),
            video_understanding_timeout_seconds=_float_env(
                source.get("VIDEO_UNDERSTANDING_TIMEOUT_SECONDS"),
                60.0,
            ),
            max_video_bytes=_int_env(source.get("MULTIMODAL_AGENT_MAX_VIDEO_BYTES"), 52_428_800),
            max_video_seconds=_float_env(source.get("MULTIMODAL_AGENT_MAX_VIDEO_SECONDS"), 60.0),
            intent_router=_intent_router(source.get("MULTIMODAL_AGENT_INTENT_ROUTER")),
            agent_graph_mode=_agent_graph_mode(source.get("AGENT_GRAPH_MODE")),
            langgraph_checkpointer_backend=_langgraph_checkpointer_backend(
                source.get("LANGGRAPH_CHECKPOINTER_BACKEND")
                or source.get("MULTIMODAL_AGENT_CHECKPOINTER_BACKEND")
            ),
            max_tool_iterations=_int_env(source.get("MAX_TOOL_ITERATIONS"), 5),
            max_plan_steps=_int_env(source.get("MAX_PLAN_STEPS"), 8),
            max_plan_revisions=_int_env(source.get("MAX_PLAN_REVISIONS"), 2),
        )
        return config

    def validate_provider_mode(self) -> None:
        """Reject a real run that would silently use a mock main LLM."""

        missing = self.resolved_chat_provider().missing_required_env()
        if self.provider_mode == "real" and (self.chat_provider == "mock" or missing):
            detail = f" Missing: {', '.join(missing)}." if missing else ""
            raise ValueError(
                "MULTIMODAL_AGENT_PROVIDER_MODE=real requires a non-mock "
                f"MULTIMODAL_AGENT_CHAT_PROVIDER with complete configuration.{detail}"
            )

    def has_any_real_provider(self) -> bool:
        return any(
            (
                self.openai_api_key,
                self.qwen_api_key,
                self.dashscope_api_key,
                self.ark_api_key,
                self.vision_embedding_api_key,
                self.qwen_vision_api_key,
                self.qwen_realtime_vision_api_key,
                self.qwen_image_api_key,
                self.ark_vision_api_key,
                self.ark_image_api_key,
                self.ark_chat_api_key,
                self.deepseek_api_key,
                self.seed_api_key,
                self.comfyui_base_url,
                self.search_api_base_url,
                self.web_search_base_url,
                self.web_search_api_key,
                self.tavily_api_key,
                self.qwen_image_search_api_key,
                self.shopping_search_api_key,
                self.shopping_compare_api_key,
                self.haodanku_api_key,
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
                    "ARK_CHAT_API_KEY": self.chat_api_key or "",
                    "ARK_API_KEY": self.chat_api_key or "",
                    "ARK_CHAT_BASE_URL": self.chat_base_url or "",
                    "ARK_CHAT_MODEL": self.chat_model or "",
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
                "ARK_API_KEY": self.ark_api_key or self.ark_chat_api_key or "",
                "ARK_CHAT_API_KEY": self.ark_chat_api_key or "",
                "ARK_CHAT_BASE_URL": self.ark_chat_base_url,
                "ARK_CHAT_MODEL": self.ark_chat_model or "",
                "DEEPSEEK_API_KEY": self.deepseek_api_key or "",
                "DEEPSEEK_CHAT_API_KEY": self.deepseek_api_key or "",
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
                    "FAKE_REALTIME_VISION_MODEL": self.vision_model or "",
                },
            )
        return resolve_vision_provider(
            self.vision_provider,
            {
                "OPENAI_API_KEY": self.openai_api_key or "",
                "OPENAI_VISION_BASE_URL": self.openai_vision_base_url,
                "OPENAI_VISION_MODEL": self.openai_vision_model,
                "QWEN_API_KEY": self.qwen_api_key or self.qwen_vision_api_key or self.dashscope_api_key or "",
                "QWEN_VISION_API_KEY": self.qwen_vision_api_key or "",
                "QWEN_VISION_BASE_URL": self.qwen_vision_base_url,
                "QWEN_VISION_MODEL": self.qwen_vision_model,
                "ARK_API_KEY": self.ark_api_key or self.ark_vision_api_key or "",
                "ARK_VISION_API_KEY": self.ark_vision_api_key or "",
                "ARK_VISION_BASE_URL": self.ark_vision_base_url,
                "ARK_VISION_MODEL": self.ark_vision_model,
                "SEED_API_KEY": self.seed_api_key or "",
                "SEED_VISION_BASE_URL": self.seed_vision_base_url,
                "SEED_VISION_MODEL": self.seed_vision_model,
                "FAKE_REALTIME_VISION_MODEL": self.vision_model or "",
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
                    "QWEN_API_KEY": self.image_generation_api_key or "",
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
                "QWEN_API_KEY": self.qwen_api_key or self.qwen_image_api_key or self.dashscope_api_key or "",
                "QWEN_IMAGE_API_KEY": self.qwen_image_api_key or "",
                "QWEN_IMAGE_BASE_URL": self.qwen_image_base_url,
                "QWEN_IMAGE_MODEL": self.qwen_image_model,
                "ARK_API_KEY": self.ark_api_key or self.ark_image_api_key or "",
                "ARK_IMAGE_API_KEY": self.ark_image_api_key or "",
                "ARK_IMAGE_BASE_URL": self.ark_image_base_url,
                "ARK_IMAGE_MODEL": self.ark_image_model,
                "COMFYUI_BASE_URL": self.comfyui_base_url or "",
                "LOCAL_IMAGE_BASE_URL": self.local_image_base_url or "",
                "LOCAL_IMAGE_MODEL": self.local_image_model,
            },
        )


def _conversation_history_backend(
    value: str | None,
) -> ConversationHistoryBackend:
    if value == "jsonl":
        return "jsonl"
    return "memory"


def _langgraph_checkpointer_backend(value: str | None) -> LangGraphCheckpointerBackend:
    if value == "none":
        return "none"
    return "memory"


def _vision_provider(value: str | None, *, allow_real: bool = True) -> VisionProviderName:
    return select_vision_provider(value, allow_real=allow_real)


def _vision_embedding_provider(value: str | None, *, allow_real: bool = True) -> VisionEmbeddingProviderName:
    if allow_real and value == "dashscope":
        return "dashscope"
    return "mock"


def _vision_embedding_api_key(source: Mapping[str, str]) -> str | None:
    return _qwen_capability_api_key(source, "QWEN_VISION_API_KEY")


def _qwen_provider_api_key(source: Mapping[str, str]) -> str | None:
    return _qwen_capability_api_key(
        source,
        "QWEN_VISION_API_KEY",
        "QWEN_IMAGE_API_KEY",
        "QWEN_IMAGE_SEARCH_API_KEY",
    )


def _ark_provider_api_key(source: Mapping[str, str]) -> str | None:
    return _ark_capability_api_key(
        source,
        "ARK_CHAT_API_KEY",
        "ARK_VISION_API_KEY",
        "ARK_IMAGE_API_KEY",
    )


def _deepseek_provider_api_key(source: Mapping[str, str]) -> str | None:
    for key_env in ("DEEPSEEK_API_KEY", "DEEPSEEK_CHAT_API_KEY"):
        value = source.get(key_env)
        if value:
            return value
    return None


def _qwen_capability_api_key(source: Mapping[str, str], *legacy_api_key_envs: str) -> str | None:
    for key_env in ("QWEN_API_KEY", "DASHSCOPE_API_KEY", *legacy_api_key_envs):
        value = source.get(key_env)
        if value:
            return value
    return None


def _ark_capability_api_key(source: Mapping[str, str], *legacy_api_key_envs: str) -> str | None:
    for key_env in ("ARK_API_KEY", *legacy_api_key_envs):
        value = source.get(key_env)
        if value:
            return value
    return None


def _qwen_chat_workspace_id(source: Mapping[str, str]) -> str | None:
    value = source.get("QWEN_CHAT_WORKSPACE_ID")
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _qwen_chat_base_url(source: Mapping[str, str], *, workspace_id: str | None) -> str:
    explicit = source.get("QWEN_CHAT_BASE_URL")
    if explicit:
        return explicit
    if workspace_id:
        return f"https://{workspace_id}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    return "https://dashscope.aliyuncs.com/compatible-mode/v1"


def _qwen_realtime_vision_workspace_id(source: Mapping[str, str]) -> str | None:
    value = source.get("QWEN_REALTIME_VISION_WORKSPACE_ID")
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _qwen_realtime_vision_region(value: str | None) -> str:
    normalized = (value or DEFAULT_QWEN_REALTIME_VISION_REGION).strip().lower()
    if normalized in SUPPORTED_QWEN_REALTIME_VISION_REGIONS:
        return normalized
    return DEFAULT_QWEN_REALTIME_VISION_REGION


def _qwen_realtime_vision_base_url(
    source: Mapping[str, str],
    *,
    workspace_id: str | None,
    region: str,
) -> str:
    explicit = source.get("QWEN_REALTIME_VISION_BASE_URL")
    if explicit:
        return explicit
    if workspace_id:
        return f"wss://{workspace_id}.{region}.maas.aliyuncs.com/api-ws/v1/realtime"
    return DEFAULT_QWEN_REALTIME_VISION_BASE_URL


def _chat_provider(value: str | None, *, allow_real: bool = True) -> ChatProviderName:
    return select_chat_provider(value, allow_real=allow_real)


def _image_generation_provider(value: str | None, *, allow_real: bool = True) -> ImageGenerationProviderName:
    return select_image_generation_provider(value, allow_real=allow_real)


def _search_provider(value: str | None, *, allow_real: bool = True) -> SearchProviderName:
    if allow_real and value in {"http", "tavily"}:
        return value
    return "mock"


def _visual_image_search_provider(value: str | None, *, allow_real: bool = True) -> VisualImageSearchProviderName:
    if allow_real and value == "qwen":
        return "qwen"
    return "mock"


def _shopping_search_provider(value: str | None, *, allow_real: bool = True) -> ShoppingSearchProviderName:
    if not allow_real:
        return "mock"
    if value in {"http", "haodanku"}:
        return value
    return "mock"


def _shopping_compare_provider(value: str | None, *, allow_real: bool = True) -> ShoppingCompareProviderName:
    if not allow_real:
        return "mock"
    if value in {"http", "haodanku"}:
        return value
    return "mock"


def _haodanku_enabled_platforms(value: str | None) -> tuple[str, ...]:
    enabled: list[str] = []
    for platform in (value or "taobao").split(","):
        normalized = platform.strip().lower()
        if normalized in {"taobao", "jd", "pdd"} and normalized not in enabled:
            enabled.append(normalized)
    return tuple(enabled) or ("taobao",)


def _clean_env_source(source: Mapping[str, str]) -> dict[str, str]:
    return {key: _clean_env_value(value) for key, value in source.items()}


def _clean_env_value(value: str) -> str:
    cleaned = value.strip()
    if " #" in cleaned:
        cleaned = cleaned.split(" #", 1)[0].strip()
    if len(cleaned) >= 2 and (cleaned[0], cleaned[-1]) in {('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’")}:
        cleaned = cleaned[1:-1]
    return cleaned.strip().strip('"').strip("'").strip("“”‘’")


def _intent_router(value: str | None) -> IntentRouterName:
    if value in {"mock_llm", "hybrid", "llm"}:
        return value
    return "rule"


def _agent_graph_mode(value: str | None) -> AgentGraphMode:
    if value == "conditional":
        return "conditional"
    return "assistant_loop"  # 默认改为 assistant_loop


def _context_compactor_mode(value: str | None) -> ContextCompactorMode:
    if value == "llm":
        return "llm"
    if value == "deterministic":
        return "deterministic"
    return "off"


def _chat_stream(source: Mapping[str, str], chat_provider: ChatProviderName) -> bool:
    provider_override = source.get("DEEPSEEK_CHAT_STREAM") if chat_provider == "deepseek" else None
    if provider_override is not None:
        return _bool_env(provider_override, True)
    if chat_provider == "deepseek":
        return _bool_env(source.get("CHAT_STREAM"), True)
    if chat_provider == "qwen":
        return _bool_env(source.get("CHAT_STREAM"), True)
    return _bool_env(source.get("CHAT_STREAM"), False)


def _bool_env(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return default


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
