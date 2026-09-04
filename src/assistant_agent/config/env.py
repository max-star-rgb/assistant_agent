"""Environment loading for nested application configuration."""

from __future__ import annotations

import os
from collections.abc import Mapping

from assistant_agent.provider_mode import ProviderMode, get_provider_mode
from assistant_agent.providers.specs import (
    resolve_chat_provider,
    resolve_image_generation_provider,
    resolve_vision_provider,
    select_chat_provider,
    select_image_generation_provider,
    select_vision_provider,
)

from .models import (
    AppConfig,
    ChatConfig,
    ImageGenerationConfig,
    LodgingConfig,
    MediaConfig,
    MemoryConfig,
    RuntimeConfig,
    SearchConfig,
    ShoppingConfig,
    ToolConfig,
    VisionConfig,
)


_QWEN_REALTIME_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
_QWEN_REALTIME_REGION = "cn-beijing"
_QWEN_IMAGE_SEARCH_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
_QWEN_IMAGE_SEARCH_MODEL = "qwen3.7-plus"


def load_app_config(env: Mapping[str, str] | None = None) -> AppConfig:
    source = _clean_env_source(os.environ if env is None else env)
    mode = get_provider_mode(source)
    _prevalidate_environment(source, mode)
    return AppConfig(
        provider_mode=mode,
        runtime=_load_runtime_config(source),
        chat=_load_chat_config(source, mode),
        vision=_load_vision_config(source, mode),
        memory=_load_memory_config(source),
        media=_load_media_config(source),
        tools=_load_tool_config(source, mode),
    )


def _prevalidate_environment(source: Mapping[str, str], mode: ProviderMode) -> None:
    """Preserve the legacy parser and validation error order before section builds."""

    allow_real = mode == "real"
    chat_provider = select_chat_provider(
        source.get("MULTIMODAL_AGENT_CHAT_PROVIDER"), allow_real=allow_real
    )
    chat_settings = resolve_chat_provider(chat_provider, source)
    select_vision_provider(
        source.get("MULTIMODAL_AGENT_VISION_PROVIDER"), allow_real=allow_real
    )
    _compatible(
        source,
        "MULTIMODAL_AGENT_EMBEDDING_PROVIDER",
        "MULTIMODAL_AGENT_VISION_EMBEDDING_PROVIDER",
        "conflicting_embedding_provider",
    )
    _compatible(
        source,
        "SIGLIP2_MODEL_DIR",
        "SIGLIP2_VISION_MODEL_DIR",
        "conflicting_siglip2_model_dir",
    )
    _reject_removed_realtime_keyframe_config(source)
    select_image_generation_provider(
        source.get("MULTIMODAL_AGENT_IMAGE_PROVIDER"), allow_real=allow_real
    )
    _workspace(source, "QWEN_REALTIME_VISION_WORKSPACE_ID")
    _realtime_region(source.get("QWEN_REALTIME_VISION_REGION"))

    # These are the only field parsers that can reject; their legacy positions
    # are after removed-key preprocessing.
    _qwen_protocol(source.get("QWEN_CHAT_API_PROTOCOL"))
    _reject_removed_runtime_config(source)

    missing = chat_settings.missing_required_env()
    if mode == "real" and (chat_provider == "mock" or missing):
        detail = f" Missing: {', '.join(missing)}." if missing else ""
        raise ValueError(
            "MULTIMODAL_AGENT_PROVIDER_MODE=real requires a non-mock "
            "MULTIMODAL_AGENT_CHAT_PROVIDER with complete configuration."
            f"{detail}"
        )

    memory_backend = source.get("MEMORY_BACKEND", "disabled")
    if memory_backend not in {"disabled", "mem0", "langmem"}:
        raise ValueError("memory backend must be disabled, mem0, or langmem")
    if _float(source.get("MEM0_TIMEOUT_SECONDS"), 5.0) <= 0:
        raise ValueError("Mem0 timeout must be positive")
    if not (source.get("MEM0_IDENTITY_NAMESPACE") or "assistant-agent").strip():
        raise ValueError("Mem0 identity namespace must be non-empty")
    if _int(source.get("MEMORY_EXTRACTION_DELAY_SECONDS"), 1800) <= 0:
        raise ValueError("memory extraction delay must be positive")

    remote_enabled = _bool(source.get("REMOTE_VISUAL_MEMORY_ENABLED"), False)
    if remote_enabled:
        if mode != "real":
            raise ValueError("remote visual memory requires provider mode real")
        if memory_backend != "langmem":
            raise ValueError("remote visual memory requires MEMORY_BACKEND=langmem")
        if not (source.get("REMOTE_VISUAL_MEMORY_BASE_URL") or "").strip():
            raise ValueError("remote visual memory requires a base URL")
    if _float(source.get("REMOTE_VISUAL_MEMORY_QUERY_TIMEOUT_SECONDS"), 5.0) <= 0:
        raise ValueError("remote visual memory query timeout must be positive")
    if _int(source.get("REMOTE_VISUAL_MEMORY_QUERY_TOP_K"), 8) <= 0:
        raise ValueError("remote visual memory top_k must be positive")
    if _float(source.get("REMOTE_VISUAL_MEMORY_SEGMENT_SECONDS"), 30.0) <= 0:
        raise ValueError("remote visual memory segment duration must be positive")
    if not source.get(
        "REMOTE_VISUAL_MEMORY_SPOOL_ROOT", ".data/remote_visual_memory"
    ).strip():
        raise ValueError("remote visual memory spool root must be non-empty")
    if _int(source.get("REMOTE_VISUAL_MEMORY_FILE_TTL_SECONDS"), 86400) <= 0:
        raise ValueError("remote visual memory file TTL must be positive")
    if _float(source.get("REMOTE_VISUAL_MEMORY_POLL_INTERVAL_SECONDS"), 2.0) <= 0:
        raise ValueError("remote visual memory poll interval must be positive")

    device = _int(source.get("SIGLIP2_CUDA_DEVICE_ID"), 0)
    if device < 0:
        raise ValueError("siglip2 CUDA device id must be non-negative")
    if device < 0:
        raise ValueError("embedding CUDA device id must be non-negative")
    if _float(source.get("REALTIME_KEYFRAME_MAX_INTERVAL_SECONDS"), 2.0) <= 0:
        raise ValueError("keyframe max interval must be positive")
    keyframe_threshold = _float(
        source.get("REALTIME_KEYFRAME_SEMANTIC_THRESHOLD"), 0.08
    )
    if not 0.0 <= keyframe_threshold <= 1.0:
        raise ValueError("keyframe semantic threshold must be between 0 and 1")
    candidate = _float(source.get("REALTIME_VISUAL_MEMORY_CANDIDATE_SIMILARITY"), 0.20)
    confirmed = _float(source.get("REALTIME_VISUAL_MEMORY_CONFIRMED_SIMILARITY"), 0.30)
    if not -1.0 <= candidate < confirmed <= 1.0:
        raise ValueError("visual memory thresholds must satisfy candidate < confirmed")
    if not source.get("VISUAL_MEMORY_QDRANT_URL", "http://127.0.0.1:6333").strip():
        raise ValueError("visual memory Qdrant URL must be non-empty")
    if not source.get(
        "VISUAL_MEMORY_QDRANT_COLLECTION", "assistant_visual_memory"
    ).strip():
        raise ValueError("visual memory Qdrant collection must be non-empty")
    if _float(source.get("VISUAL_MEMORY_QDRANT_TIMEOUT_SECONDS"), 2.0) <= 0:
        raise ValueError("visual memory Qdrant timeout must be positive")
    if not source.get(
        "VISUAL_MEMORY_DENSE_MODEL_CACHE_DIR", ".data/models/fastembed"
    ).strip():
        raise ValueError("visual memory dense model cache must be non-empty")
    if (
        not 0.0
        <= _float(source.get("REALTIME_VISUAL_REMINDER_SIMILARITY_THRESHOLD"), 0.82)
        <= 1.0
    ):
        raise ValueError("visual reminder similarity threshold must be within [0, 1]")
    if _int(source.get("REALTIME_VISUAL_REMINDER_MAX_ACTIVE"), 16) <= 0:
        raise ValueError("visual reminder active limit must be positive")
    if _int(source.get("REALTIME_VISUAL_REMINDER_TERMINAL_HISTORY_LIMIT"), 64) <= 0:
        raise ValueError("visual reminder terminal history limit must be positive")

    if _float(source.get("PROACTIVE_MESSAGE_DELIVERY_TIMEOUT_SECONDS"), 95.0) <= 0:
        raise ValueError("proactive message delivery timeout must be positive")
    if not (
        source.get("PROACTIVE_DELIVERY_STORE_PATH")
        or ".local/agent_server/proactive_deliveries.sqlite3"
    ).strip():
        raise ValueError("proactive delivery store path must be non-empty")
    if _float(source.get("PROACTIVE_DELIVERY_ACK_TIMEOUT_SECONDS"), 15.0) <= 0:
        raise ValueError("proactive delivery ACK timeout must be positive")
    if _float(source.get("PROACTIVE_DELIVERY_LEASE_SECONDS"), 30.0) <= 0:
        raise ValueError("proactive delivery lease must be positive")
    if _float(source.get("PROACTIVE_DELIVERY_PRESENCE_TTL_SECONDS"), 45.0) <= 0:
        raise ValueError("proactive delivery presence TTL must be positive")
    if _float(source.get("PROACTIVE_DELIVERY_POLL_INTERVAL_SECONDS"), 0.25) <= 0:
        raise ValueError("proactive delivery poll interval must be positive")

    context_target = _ratio(
        source.get("MULTIMODAL_AGENT_CONTEXT_COMPACTION_TARGET_RATIO"), 0.15
    )
    context_trigger = _ratio(
        source.get("MULTIMODAL_AGENT_CONTEXT_COMPACTION_TRIGGER_RATIO"), 0.75
    )
    context_hard = _ratio(
        source.get("MULTIMODAL_AGENT_CONTEXT_COMPACTION_HARD_RATIO"), 0.85
    )
    if not 0.0 < context_target < context_trigger < context_hard <= 1.0:
        raise ValueError(
            "context compaction ratios must satisfy 0 < target < trigger < hard <= 1"
        )
    visual_target = _ratio(
        source.get("REALTIME_VISUAL_CONTEXT_COMPACTION_TARGET_RATIO"), 0.40
    )
    visual_trigger = _ratio(
        source.get("REALTIME_VISUAL_CONTEXT_COMPACTION_TRIGGER_RATIO"), 0.70
    )
    visual_hard = _ratio(
        source.get("REALTIME_VISUAL_CONTEXT_COMPACTION_HARD_RATIO"), 0.85
    )
    if not 0.0 < visual_target < visual_trigger < visual_hard <= 1.0:
        raise ValueError(
            "visual context compaction ratios must satisfy "
            "0 < target < trigger < hard <= 1"
        )


def _load_runtime_config(source: Mapping[str, str]) -> RuntimeConfig:
    return RuntimeConfig(
        current_location=source.get("MULTIMODAL_AGENT_CURRENT_LOCATION")
        or "上海市青浦区华为练秋湖研发中心",
    )


def _load_chat_config(source: Mapping[str, str], mode: ProviderMode) -> ChatConfig:
    provider = select_chat_provider(
        source.get("MULTIMODAL_AGENT_CHAT_PROVIDER"), allow_real=mode == "real"
    )
    resolved = resolve_chat_provider(provider, source)
    stream = _chat_stream(source, provider)
    return ChatConfig(
        chat_provider=provider,
        chat_api_key=resolved.api_key,
        chat_base_url=resolved.base_url,
        chat_model=resolved.model,
        chat_stream=stream,
        native_provider_streaming=_bool(
            source.get("MULTIMODAL_AGENT_NATIVE_PROVIDER_STREAMING"),
            provider == "qwen" and stream,
        ),
        chat_timeout_seconds=_float(
            source.get("MULTIMODAL_AGENT_CHAT_TIMEOUT_SECONDS"), 75.0
        ),
        deep_research_chat_max_tokens=max(
            1, _int(source.get("MULTIMODAL_AGENT_DEEP_RESEARCH_MAX_TOKENS"), 8192)
        ),
        context_compactor_mode="llm"
        if source.get("MULTIMODAL_AGENT_CONTEXT_COMPACTOR") == "llm"
        else "off",
        context_tokenizer_path=source.get("MULTIMODAL_AGENT_CONTEXT_TOKENIZER_PATH")
        or None,
        context_input_token_limit=max(
            8192,
            _int(
                source.get("MULTIMODAL_AGENT_CONTEXT_INPUT_TOKEN_LIMIT"),
                _default_context_limit(resolved.model),
            ),
        ),
        context_compaction_trigger_ratio=_ratio(
            source.get("MULTIMODAL_AGENT_CONTEXT_COMPACTION_TRIGGER_RATIO"), 0.75
        ),
        context_compaction_target_ratio=_ratio(
            source.get("MULTIMODAL_AGENT_CONTEXT_COMPACTION_TARGET_RATIO"), 0.15
        ),
        context_compaction_hard_ratio=_ratio(
            source.get("MULTIMODAL_AGENT_CONTEXT_COMPACTION_HARD_RATIO"), 0.85
        ),
        context_compaction_safety_margin_tokens=max(
            0, _int(source.get("MULTIMODAL_AGENT_CONTEXT_SAFETY_MARGIN_TOKENS"), 50000)
        ),
        context_summary_max_tokens=max(
            512, _int(source.get("MULTIMODAL_AGENT_CONTEXT_SUMMARY_MAX_TOKENS"), 32768)
        ),
        qwen_chat_enable_thinking=_bool(source.get("QWEN_CHAT_ENABLE_THINKING"), False),
        qwen_chat_enable_search=_bool(source.get("QWEN_CHAT_ENABLE_SEARCH"), False),
        qwen_chat_api_protocol=_qwen_protocol(source.get("QWEN_CHAT_API_PROTOCOL")),
    )


def _load_vision_config(source: Mapping[str, str], mode: ProviderMode) -> VisionConfig:
    allow_real = mode == "real"
    provider = select_vision_provider(
        source.get("MULTIMODAL_AGENT_VISION_PROVIDER"), allow_real=allow_real
    )
    resolved = resolve_vision_provider(provider, source)
    embedding = _compatible(
        source,
        "MULTIMODAL_AGENT_EMBEDDING_PROVIDER",
        "MULTIMODAL_AGENT_VISION_EMBEDDING_PROVIDER",
        "conflicting_embedding_provider",
    )
    embedding = (
        embedding
        if allow_real and embedding in {"dashscope", "local_siglip2"}
        else "mock"
    )
    model_dir = _compatible(
        source,
        "SIGLIP2_MODEL_DIR",
        "SIGLIP2_VISION_MODEL_DIR",
        "conflicting_siglip2_model_dir",
    )
    workspace = _workspace(source, "QWEN_REALTIME_VISION_WORKSPACE_ID")
    region = _realtime_region(source.get("QWEN_REALTIME_VISION_REGION"))
    device = _int(source.get("SIGLIP2_CUDA_DEVICE_ID"), 0)
    return VisionConfig(
        qwen_realtime_vision_api_key=resolved.api_key if provider == "qwen" else None,
        vision_provider=provider,
        vision_api_key=resolved.api_key,
        vision_base_url=resolved.base_url,
        vision_model=resolved.model,
        vision_adapter_kind=resolved.adapter_kind,
        vision_embedding_provider=embedding,
        embedding_provider=embedding,
        vision_embedding_api_key=_qwen_key(source, "QWEN_VISION_API_KEY")
        if embedding == "dashscope"
        else None,
        vision_embedding_base_url=source.get(
            "DASHSCOPE_MULTIMODAL_EMBEDDING_BASE_URL",
            "https://dashscope.aliyuncs.com/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding",
        ),
        vision_embedding_model=source.get(
            "DASHSCOPE_VISION_EMBEDDING_MODEL",
            "tongyi-embedding-vision-flash-2026-03-06",
        ),
        vision_embedding_dimension=_int(
            source.get("DASHSCOPE_VISION_EMBEDDING_DIMENSION"), 768
        ),
        vision_embedding_timeout_seconds=_float(
            source.get("DASHSCOPE_VISION_EMBEDDING_TIMEOUT_SECONDS"), 30.0
        ),
        siglip2_vision_model_dir=model_dir,
        siglip2_cuda_device_id=device,
        siglip2_model_dir=model_dir,
        embedding_cuda_device_id=device,
        keyframe_max_interval_seconds=_float(
            source.get("REALTIME_KEYFRAME_MAX_INTERVAL_SECONDS"), 2.0
        ),
        keyframe_semantic_threshold=_float(
            source.get("REALTIME_KEYFRAME_SEMANTIC_THRESHOLD"), 0.08
        ),
        visual_memory_candidate_similarity=_float(
            source.get("REALTIME_VISUAL_MEMORY_CANDIDATE_SIMILARITY"), 0.20
        ),
        visual_memory_confirmed_similarity=_float(
            source.get("REALTIME_VISUAL_MEMORY_CONFIRMED_SIMILARITY"), 0.30
        ),
        visual_memory_qdrant_url=source.get(
            "VISUAL_MEMORY_QDRANT_URL", "http://127.0.0.1:6333"
        ),
        visual_memory_qdrant_collection=source.get(
            "VISUAL_MEMORY_QDRANT_COLLECTION", "assistant_visual_memory"
        ),
        visual_memory_qdrant_timeout_seconds=_float(
            source.get("VISUAL_MEMORY_QDRANT_TIMEOUT_SECONDS"), 2.0
        ),
        visual_memory_dense_model_cache_dir=source.get(
            "VISUAL_MEMORY_DENSE_MODEL_CACHE_DIR", ".data/models/fastembed"
        ),
        visual_memory_result_limit=12,
        visual_reminder_similarity_threshold=_float(
            source.get("REALTIME_VISUAL_REMINDER_SIMILARITY_THRESHOLD"), 0.82
        ),
        visual_reminder_max_active=_int(
            source.get("REALTIME_VISUAL_REMINDER_MAX_ACTIVE"), 16
        ),
        visual_reminder_terminal_history_limit=_int(
            source.get("REALTIME_VISUAL_REMINDER_TERMINAL_HISTORY_LIMIT"), 64
        ),
        qwen_realtime_vision_base_url=_realtime_url(source, workspace, region),
        qwen_realtime_vision_model=source.get(
            "QWEN_REALTIME_VISION_MODEL", "qwen3.5-omni-flash-realtime"
        ),
        visual_context_compactor_mode="llm"
        if source.get("REALTIME_VISUAL_CONTEXT_COMPACTOR") == "llm"
        else "off",
        visual_context_input_token_limit=max(
            1, _int(source.get("REALTIME_VISUAL_CONTEXT_INPUT_TOKEN_LIMIT"), 32768)
        ),
        visual_context_compaction_target_ratio=_ratio(
            source.get("REALTIME_VISUAL_CONTEXT_COMPACTION_TARGET_RATIO"), 0.40
        ),
        visual_context_compaction_trigger_ratio=_ratio(
            source.get("REALTIME_VISUAL_CONTEXT_COMPACTION_TRIGGER_RATIO"), 0.70
        ),
        visual_context_compaction_hard_ratio=_ratio(
            source.get("REALTIME_VISUAL_CONTEXT_COMPACTION_HARD_RATIO"), 0.85
        ),
        visual_context_compaction_safety_margin_tokens=max(
            0, _int(source.get("REALTIME_VISUAL_CONTEXT_SAFETY_MARGIN_TOKENS"), 2048)
        ),
        visual_context_summary_max_tokens=max(
            1, _int(source.get("REALTIME_VISUAL_CONTEXT_SUMMARY_MAX_TOKENS"), 2048)
        ),
    )


def _load_memory_config(source: Mapping[str, str]) -> MemoryConfig:
    return MemoryConfig(
        mem0_base_url=source.get("MEM0_BASE_URL"),
        mem0_api_key=source.get("MEM0_API_KEY"),
        mem0_timeout_seconds=_float(source.get("MEM0_TIMEOUT_SECONDS"), 5.0),
        mem0_identity_namespace=source.get("MEM0_IDENTITY_NAMESPACE")
        or "assistant-agent",
        memory_backend=source.get("MEMORY_BACKEND", "disabled"),
        memory_extraction_delay_seconds=_int(
            source.get("MEMORY_EXTRACTION_DELAY_SECONDS"), 1800
        ),
        langmem_model=source.get("LANGMEM_MODEL"),
    )


def _reject_removed_realtime_keyframe_config(source: Mapping[str, str]) -> None:
    if any(
        name in source
        for name in (
            "REALTIME_KEYFRAME_STRUCTURAL_THRESHOLD",
            "REALTIME_KEYFRAME_COMBINED_THRESHOLD",
            "REALTIME_SEMANTIC_INPUT_FPS",
            "REALTIME_KEYFRAME_SEMANTIC_PROBE_FPS",
            "REALTIME_KEYFRAME_MIN_INTERVAL_SECONDS",
        )
    ):
        raise ValueError("removed_realtime_keyframe_config")


def _reject_removed_runtime_config(source: Mapping[str, str]) -> None:
    if any(
        name in source
        for name in (
            "ASSISTANT_AGENT_TEXT_TURN_TIMEOUT_SECONDS",
            "LANGGRAPH_CHECKPOINTER_BACKEND",
            "MULTIMODAL_AGENT_CHECKPOINTER_BACKEND",
            "LANGGRAPH_CHECKPOINT_PATH",
            "MEMORY_COMMIT_LEDGER_PATH",
            "MULTIMODAL_AGENT_CONVERSATION_HISTORY_BACKEND",
            "MULTIMODAL_AGENT_CONVERSATION_HISTORY_PATH",
            "MULTIMODAL_AGENT_MAX_CONVERSATION_HISTORY_TURNS",
            "MULTIMODAL_AGENT_MAX_CONVERSATION_TURNS",
            "MULTIMODAL_AGENT_EDITABLE_CONTEXT_ENABLED",
            "MULTIMODAL_AGENT_EDITABLE_CONTEXT_ROOT",
            "MULTIMODAL_AGENT_EDITABLE_CONTEXT_USER_ID",
        )
    ):
        raise ValueError("removed_runtime_config")


def _load_media_config(source: Mapping[str, str]) -> MediaConfig:
    return MediaConfig(
        proactive_message_delivery_timeout_seconds=_float(
            source.get("PROACTIVE_MESSAGE_DELIVERY_TIMEOUT_SECONDS"), 95.0
        ),
        proactive_delivery_store_path=source.get("PROACTIVE_DELIVERY_STORE_PATH")
        or ".local/agent_server/proactive_deliveries.sqlite3",
        proactive_delivery_ack_timeout_seconds=_float(
            source.get("PROACTIVE_DELIVERY_ACK_TIMEOUT_SECONDS"), 15.0
        ),
        proactive_delivery_lease_seconds=_float(
            source.get("PROACTIVE_DELIVERY_LEASE_SECONDS"), 30.0
        ),
        proactive_delivery_presence_ttl_seconds=_float(
            source.get("PROACTIVE_DELIVERY_PRESENCE_TTL_SECONDS"), 45.0
        ),
        proactive_delivery_poll_interval_seconds=_float(
            source.get("PROACTIVE_DELIVERY_POLL_INTERVAL_SECONDS"), 0.25
        ),
        remote_visual_memory_enabled=_bool(
            source.get("REMOTE_VISUAL_MEMORY_ENABLED"), False
        ),
        remote_visual_memory_base_url=source.get("REMOTE_VISUAL_MEMORY_BASE_URL"),
        remote_visual_memory_query_timeout_seconds=_float(
            source.get("REMOTE_VISUAL_MEMORY_QUERY_TIMEOUT_SECONDS"), 5.0
        ),
        remote_visual_memory_query_top_k=_int(
            source.get("REMOTE_VISUAL_MEMORY_QUERY_TOP_K"), 8
        ),
        remote_visual_memory_download_base_url=source.get(
            "REMOTE_VISUAL_MEMORY_DOWNLOAD_BASE_URL"
        ),
        remote_visual_memory_segment_seconds=_float(
            source.get("REMOTE_VISUAL_MEMORY_SEGMENT_SECONDS"), 30.0
        ),
        remote_visual_memory_spool_root=source.get(
            "REMOTE_VISUAL_MEMORY_SPOOL_ROOT", ".data/remote_visual_memory"
        ),
        remote_visual_memory_file_ttl_seconds=_int(
            source.get("REMOTE_VISUAL_MEMORY_FILE_TTL_SECONDS"), 86400
        ),
        remote_visual_memory_poll_interval_seconds=_float(
            source.get("REMOTE_VISUAL_MEMORY_POLL_INTERVAL_SECONDS"), 2.0
        ),
        td_gen_ip=source.get("TD_GEN_IP"),
        td_gen_port=_positive_int(source.get("TD_GEN_PORT")),
        public_ip=source.get("PUBLIC_IP"),
        public_port=_positive_int(source.get("PUBLIC_PORT")),
        image_to_3d_timeout_seconds=_float(
            source.get("IMAGE_TO_3D_TIMEOUT_SECONDS"), 5.0
        ),
        video_understanding_timeout_seconds=_float(
            source.get("VIDEO_UNDERSTANDING_TIMEOUT_SECONDS"), 60.0
        ),
        max_video_bytes=_int(source.get("MULTIMODAL_AGENT_MAX_VIDEO_BYTES"), 52428800),
        max_video_seconds=_float(
            source.get("MULTIMODAL_AGENT_MAX_VIDEO_SECONDS"), 60.0
        ),
    )


def _load_tool_config(source: Mapping[str, str], mode: ProviderMode) -> ToolConfig:
    allow_real = mode == "real"
    provider = select_image_generation_provider(
        source.get("MULTIMODAL_AGENT_IMAGE_PROVIDER"), allow_real=allow_real
    )
    resolved = resolve_image_generation_provider(provider, source)
    shopping_provider = source.get("MULTIMODAL_AGENT_SHOPPING_PROVIDER")
    return ToolConfig(
        durable_tasks_enabled=_bool(
            source.get("MULTIMODAL_AGENT_DURABLE_TASKS_ENABLED"), False
        ),
        image_generation=ImageGenerationConfig(
            image_generation_provider=provider,
            image_generation_api_key=resolved.api_key,
            image_generation_base_url=resolved.base_url,
            image_generation_model=resolved.model,
            image_generation_adapter_kind=resolved.adapter_kind,
            qwen_image_default_size=source.get("QWEN_IMAGE_DEFAULT_SIZE", "1024*1024"),
        ),
        search=SearchConfig(
            visual_image_search_provider="qwen"
            if allow_real
            and source.get("MULTIMODAL_AGENT_VISUAL_IMAGE_SEARCH_PROVIDER") == "qwen"
            else "mock",
            qwen_image_search_api_key=_qwen_key(source, "QWEN_IMAGE_SEARCH_API_KEY")
            if allow_real
            and source.get("MULTIMODAL_AGENT_VISUAL_IMAGE_SEARCH_PROVIDER") == "qwen"
            else None,
            qwen_image_search_base_url=source.get(
                "QWEN_IMAGE_SEARCH_BASE_URL", _QWEN_IMAGE_SEARCH_URL
            ),
            qwen_image_search_model=source.get(
                "QWEN_IMAGE_SEARCH_MODEL", _QWEN_IMAGE_SEARCH_MODEL
            ),
            qwen_image_search_timeout_seconds=_float(
                source.get("QWEN_IMAGE_SEARCH_TIMEOUT_SECONDS"), 30.0
            ),
        ),
        shopping=ShoppingConfig(
            shopping_search_provider=shopping_provider
            if allow_real and shopping_provider in {"http", "haodanku"}
            else "mock",
            shopping_search_base_url=source.get("SHOPPING_SEARCH_BASE_URL")
            or source.get("SEARCH_API_BASE_URL"),
            shopping_search_api_key=source.get("SHOPPING_SEARCH_API_KEY"),
            shopping_search_timeout_seconds=_float(
                source.get("SHOPPING_SEARCH_TIMEOUT_SECONDS"), 10.0
            ),
            shopping_compare_provider=shopping_provider
            if allow_real and shopping_provider in {"http", "haodanku"}
            else "mock",
            shopping_compare_base_url=source.get("SHOPPING_COMPARE_BASE_URL"),
            shopping_compare_api_key=source.get("SHOPPING_COMPARE_API_KEY"),
            shopping_compare_timeout_seconds=_float(
                source.get("SHOPPING_COMPARE_TIMEOUT_SECONDS"), 10.0
            ),
            haodanku_api_key=source.get("HAODANKU_API_KEY"),
            haodanku_base_url=source.get("HAODANKU_BASE_URL")
            or "https://v3.api.haodanku.com",
            haodanku_timeout_seconds=_float(
                source.get("HAODANKU_TIMEOUT_SECONDS"), 10.0
            ),
            haodanku_enabled_platforms=_platforms(
                source.get("HAODANKU_ENABLED_PLATFORMS")
            ),
            haodanku_taobao_pid=source.get("HAODANKU_TAOBAO_PID"),
            haodanku_taobao_authorized_name=source.get(
                "HAODANKU_TAOBAO_AUTHORIZED_NAME"
            ),
            haodanku_jd_sub_union_id=source.get("HAODANKU_JD_SUB_UNION_ID"),
            haodanku_pdd_channel=source.get("HAODANKU_PDD_CHANNEL"),
        ),
        lodging=LodgingConfig(
            lodging_provider="flyai"
            if allow_real and source.get("MULTIMODAL_AGENT_LODGING_PROVIDER") == "flyai"
            else "mock",
            flyai_cli_path=source.get("FLYAI_CLI_PATH"),
            flyai_api_key=source.get("FLYAI_API_KEY"),
            flyai_timeout_seconds=_float(source.get("FLYAI_TIMEOUT_SECONDS"), 30.0),
        ),
    )


def _clean_env_source(source: Mapping[str, str]) -> dict[str, str]:
    return {key: _clean_env_value(value) for key, value in source.items()}


def _clean_env_value(value: str) -> str:
    value = value.strip()
    if " #" in value:
        value = value.split(" #", 1)[0].strip()
    if len(value) >= 2 and (value[0], value[-1]) in {
        ('"', '"'),
        ("'", "'"),
        ("“", "”"),
        ("‘", "’"),
    }:
        value = value[1:-1]
    return value.strip().strip('"').strip("'").strip("“”‘’")


def _workspace(source: Mapping[str, str], name: str) -> str | None:
    return (source.get(name) or "").strip() or None


def _qwen_key(source: Mapping[str, str], *legacy: str) -> str | None:
    for name in ("QWEN_API_KEY", "DASHSCOPE_API_KEY", *legacy):
        if value := source.get(name):
            return value
    return None


def _compatible(
    source: Mapping[str, str], canonical: str, legacy: str, error: str
) -> str | None:
    current, old = source.get(canonical), source.get(legacy)
    if current is not None and old is not None and current != old:
        raise ValueError(error)
    return current if current is not None else old


def _realtime_region(value: str | None) -> str:
    value = (value or _QWEN_REALTIME_REGION).strip().lower()
    return value if value in {"cn-beijing", "ap-southeast-1"} else _QWEN_REALTIME_REGION


def _realtime_url(source: Mapping[str, str], workspace: str | None, region: str) -> str:
    if value := source.get("QWEN_REALTIME_VISION_BASE_URL"):
        return value
    if workspace:
        return f"wss://{workspace}.{region}.maas.aliyuncs.com/api-ws/v1/realtime"
    return _QWEN_REALTIME_URL


def _qwen_protocol(value: str | None) -> str:
    value = (value or "dashscope").strip().lower()
    if value not in {"dashscope", "openai_compatible"}:
        raise ValueError(
            "QWEN_CHAT_API_PROTOCOL must be 'dashscope' or 'openai_compatible'"
        )
    return value


def _platforms(value: str | None) -> tuple[str, ...]:
    result: list[str] = []
    for item in (value or "taobao").split(","):
        item = item.strip().lower()
        if item in {"taobao", "jd", "pdd"} and item not in result:
            result.append(item)
    return tuple(result) or ("taobao",)


def _default_context_limit(model: str | None) -> int:
    return (
        1_000_000
        if (model or "")
        .strip()
        .lower()
        .startswith(("qwen3.6-flash", "deepseek-v4-flash"))
        else 128_000
    )


def _ratio(value: str | None, default: float) -> float:
    try:
        return min(1.0, max(0.0, float(value) if value is not None else default))
    except ValueError:
        return default


def _chat_stream(source: Mapping[str, str], provider: str) -> bool:
    if (
        provider == "deepseek"
        and (value := source.get("DEEPSEEK_CHAT_STREAM")) is not None
    ):
        return _bool(value, True)
    return _bool(source.get("CHAT_STREAM"), provider in {"deepseek", "qwen"})


def _bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return (
        True
        if value.strip().lower() in {"1", "true", "yes", "y", "on"}
        else False
        if value.strip().lower() in {"0", "false", "no", "n", "off"}
        else default
    )


def _float(value: str | None, default: float) -> float:
    try:
        return float(value) if value is not None else default
    except ValueError:
        return default


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


def _positive_int(value: str | None) -> int | None:
    parsed = _int(value, 0)
    return parsed if parsed > 0 else None
