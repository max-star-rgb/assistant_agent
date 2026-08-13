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



def test_langmem_backend_binds_the_configured_openai_compatible_provider(
    monkeypatch,
) -> None:
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ALL_PROXY", "socks://127.0.0.1:19090")
    captured = {}
    expected_bundle = object()

    def capture_bundle(*, model, store, ledger, aclose=None):
        captured["model"] = model
        captured["store"] = store
        captured["ledger"] = ledger
        captured["aclose"] = aclose
        return expected_bundle

    monkeypatch.setattr(
        "assistant_agent.memory.factory.create_langmem_memory_bundle",
        capture_bundle,
    )
    store = InMemoryStore()
    bundle = create_memory_node_bundle(
        ProviderConfig(
            provider_mode="real",
            chat_provider="qwen",
            chat_api_key="provider-key-sentinel",
            chat_base_url="https://provider.example/v1",
            chat_model="assistant-model",
            memory_backend="langmem",
            langmem_model="memory-model",
        ),
        langmem_store=store,
    )

    model = captured["model"]
    assert bundle is expected_bundle
    assert model.model_name == "memory-model"
    assert model.openai_api_base == "https://provider.example/v1"
    assert model.openai_api_key.get_secret_value() == "provider-key-sentinel"
    assert model.http_client is not None
    assert model.http_async_client is not None
    assert model.http_socket_options == ()
    assert callable(captured["aclose"])
    assert captured["store"] is store


def test_unknown_memory_backend_is_rejected_by_trusted_config() -> None:
    with pytest.raises(ValueError):
        ProviderConfig(memory_backend="unknown")  # type: ignore[arg-type]
