from __future__ import annotations

import pytest
from langgraph.store.memory import InMemoryStore

from assistant_agent.config import ProviderConfig
from assistant_agent.memory.factory import (
    MemoryBackendConfigurationError,
    create_memory_node_bundle,
)


def _real_config(**updates):
    return ProviderConfig(
        provider_mode="real",
        chat_provider="local",
        local_chat_base_url="http://127.0.0.1:9998/v1",
        local_chat_model="local-test-model",
        **updates,
    )


def test_memory_factory_defaults_to_one_disabled_bundle() -> None:
    bundle = create_memory_node_bundle(ProviderConfig())

    assert bundle.backend_id == "disabled"
    assert bundle.store is None


def test_mem0_backend_requires_real_mode_and_complete_explicit_config() -> None:
    with pytest.raises(MemoryBackendConfigurationError, match="real provider mode"):
        create_memory_node_bundle(ProviderConfig(memory_backend="mem0"))

    with pytest.raises(MemoryBackendConfigurationError, match="MEM0_BASE_URL"):
        create_memory_node_bundle(_real_config(memory_backend="mem0"))

    bundle = create_memory_node_bundle(
        _real_config(
            memory_backend="mem0",
            mem0_base_url="http://127.0.0.1:9999",
        )
    )
    assert bundle.backend_id == "mem0"
    assert bundle.store is None


def test_langmem_backend_requires_explicit_store_before_optional_import() -> None:
    config = _real_config(
        memory_backend="langmem",
        langmem_model="fake:model",
    )

    with pytest.raises(MemoryBackendConfigurationError, match="BaseStore"):
        create_memory_node_bundle(config)

    # A Store satisfies composition, after which the absent optional package
    # fails closed in the LangMem-specific configuration boundary.
    with pytest.raises(MemoryBackendConfigurationError, match="optional dependency"):
        create_memory_node_bundle(config, langmem_store=InMemoryStore())


def test_unknown_memory_backend_is_rejected_by_trusted_config() -> None:
    with pytest.raises(ValueError):
        ProviderConfig(memory_backend="unknown")  # type: ignore[arg-type]
