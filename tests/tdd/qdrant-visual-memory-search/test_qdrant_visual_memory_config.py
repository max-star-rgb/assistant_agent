from assistant_agent.config import ProviderConfig


def test_qdrant_visual_memory_config_is_explicit_and_local_by_default() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_PROVIDER_MODE": "mock",
            "VISUAL_MEMORY_QDRANT_URL": "http://127.0.0.1:6333",
            "VISUAL_MEMORY_QDRANT_COLLECTION": "assistant-visual-memory",
            "VISUAL_MEMORY_QDRANT_TIMEOUT_SECONDS": "2.5",
            "VISUAL_MEMORY_DENSE_MODEL_CACHE_DIR": "/models/fastembed",
        }
    )

    assert config.visual_memory_qdrant_url == "http://127.0.0.1:6333"
    assert config.visual_memory_qdrant_collection == "assistant-visual-memory"
    assert config.visual_memory_qdrant_timeout_seconds == 2.5
    assert config.visual_memory_dense_model_cache_dir == "/models/fastembed"
    assert config.visual_memory_result_limit == 12
