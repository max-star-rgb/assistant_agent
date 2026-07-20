from assistant_agent.config import ProviderConfig
from assistant_agent.memory import factory as memory_factory
from assistant_agent.memory.factory import create_memory_store
from assistant_agent.memory.framework import (
    HindsightMemoryEngineAdapter,
    Mem0MemoryEngineAdapter,
)
from assistant_agent.memory.framework.store import FrameworkMemoryStore
from assistant_agent.memory.sqlite_store import SQLiteMemoryStore
from assistant_agent.memory.store import InMemoryStore
from assistant_agent.services.memory_core_status import build_memory_core_status


def test_framework_backend_requires_dedicated_explicit_opt_in() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_MEMORY_BACKEND": "framework",
            "MULTIMODAL_AGENT_MEMORY_FRAMEWORK": "hindsight",
            "MEMORY_FRAMEWORK_BASE_URL": "http://hindsight.local",
            "OPENAI_API_KEY": "present-but-must-not-enable",
        }
    )

    assert config.memory_backend == "memory"
    assert isinstance(create_memory_store(config), InMemoryStore)


def test_hindsight_framework_config_is_explicit_and_versioned() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_MEMORY_BACKEND": "framework",
            "MULTIMODAL_AGENT_MEMORY_FRAMEWORK_ENABLED": "true",
            "MULTIMODAL_AGENT_MEMORY_FRAMEWORK": "hindsight",
            "MEMORY_FRAMEWORK_BASE_URL": "http://hindsight.local",
            "MEMORY_FRAMEWORK_TIMEOUT_SECONDS": "7.5",
            "MEMORY_FRAMEWORK_IDENTITY_NAMESPACE": "pilot-a",
            "MEMORY_FRAMEWORK_LEDGER_PATH": ".local/test-ledger.sqlite3",
        }
    )

    assert config.memory_backend == "framework"
    assert config.memory_framework == "hindsight"
    assert config.memory_framework_version == "0.8.4"
    assert config.memory_framework_base_url == "http://hindsight.local"
    assert config.memory_framework_timeout_seconds == 7.5
    assert config.memory_framework_identity_namespace == "pilot-a"


def test_framework_enabled_selects_framework_backend_without_backend_env(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(memory_factory, "REPO_ROOT", tmp_path)
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_MEMORY_FRAMEWORK_ENABLED": "true",
            "MEMORY_FRAMEWORK_BASE_URL": "http://mem0.local",
            "MEMORY_FRAMEWORK_LEDGER_PATH": "ledger.sqlite3",
        }
    )

    store = create_memory_store(config)

    assert config.memory_backend == "framework"
    assert config.memory_framework == "mem0"
    assert isinstance(store, FrameworkMemoryStore)
    assert isinstance(store.adapter, Mem0MemoryEngineAdapter)


def test_framework_opt_in_defaults_to_mem0_pilot_engine(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(memory_factory, "REPO_ROOT", tmp_path)
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_MEMORY_BACKEND": "framework",
            "MULTIMODAL_AGENT_MEMORY_FRAMEWORK_ENABLED": "true",
            "MEMORY_FRAMEWORK_BASE_URL": "http://mem0.local",
            "MEMORY_FRAMEWORK_LEDGER_PATH": "ledger.sqlite3",
        }
    )

    store = create_memory_store(config)

    assert config.memory_backend == "framework"
    assert config.memory_framework == "mem0"
    assert config.memory_framework_version == "2.0.11"
    assert isinstance(store, FrameworkMemoryStore)
    assert isinstance(store.adapter, Mem0MemoryEngineAdapter)


def test_factory_builds_mem0_with_read_only_sqlite_fallback(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(memory_factory, "REPO_ROOT", tmp_path)
    store = create_memory_store(
        ProviderConfig(
            memory_backend="framework",
            memory_framework="mem0",
            memory_framework_base_url="http://mem0.local",
            memory_framework_ledger_path="ledger.sqlite3",
            memory_framework_fallback_backend="sqlite",
            memory_path="legacy.sqlite3",
        )
    )

    assert isinstance(store, FrameworkMemoryStore)
    assert isinstance(store.adapter, Mem0MemoryEngineAdapter)
    assert isinstance(store.read_fallback, SQLiteMemoryStore)
    assert store.ledger.path == tmp_path / "ledger.sqlite3"


def test_factory_builds_hindsight_without_implicit_fallback(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(memory_factory, "REPO_ROOT", tmp_path)
    store = create_memory_store(
        ProviderConfig(
            memory_backend="framework",
            memory_framework="hindsight",
            memory_framework_base_url="http://hindsight.local",
            memory_framework_ledger_path="ledger.sqlite3",
        )
    )

    assert isinstance(store, FrameworkMemoryStore)
    assert isinstance(store.adapter, HindsightMemoryEngineAdapter)
    assert store.read_fallback is None


def test_framework_core_status_is_prompt_safe_and_names_engine(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(memory_factory, "REPO_ROOT", tmp_path)
    config = ProviderConfig(
        memory_backend="framework",
        memory_framework="mem0",
        memory_framework_base_url="http://secret-sidecar.internal",
        memory_framework_ledger_path="ledger.sqlite3",
    )
    store = create_memory_store(config)

    status = build_memory_core_status(
        config=config,
        memory_store=store,
        remote_errors=[{"code": "memory_framework_recall_failed"}],
    )

    payload = status.model_dump(mode="json")
    assert payload["mode"] == "framework"
    assert payload["external_core"] == "mem0"
    assert payload["external_lifecycle_owner"] is True
    assert payload["remote_error_codes"] == ["memory_framework_recall_failed"]
    assert "secret-sidecar.internal" not in str(payload)
