from datetime import datetime, timezone

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.memory import factory as memory_factory
from assistant_agent.memory.factory import create_memory_store
from assistant_agent.memory.jsonl_store import JsonlMemoryStore
import pytest

from assistant_agent.memory.remote import (
    HybridMemoryStore,
    MemoryServerRequest,
    MemoryServiceOperationError,
    RemoteMemoryClient,
    RemoteServiceMemoryStore,
)
from assistant_agent.memory.sqlite_store import SQLiteMemoryStore
from assistant_agent.memory.store import InMemoryStore
from assistant_agent.memory.write_policy import MemoryWritePolicy
from assistant_agent.schemas.memory import MemoryItem
from assistant_agent.schemas.requests import UserRequest


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def runtime_with_auto_memory(**kwargs) -> AgentGraphRuntime:
    runtime = AgentGraphRuntime(**kwargs)
    runtime.memory_manager.write_policy = MemoryWritePolicy(allow_auto_write=True)
    return runtime


def test_default_memory_backend_is_in_memory() -> None:
    store = create_memory_store(ProviderConfig.from_env({}))

    assert isinstance(store, InMemoryStore)


def test_hybrid_remote_memory_backend_requires_explicit_remote_opt_in() -> None:
    config = ProviderConfig.from_env({"MULTIMODAL_AGENT_MEMORY_BACKEND": "hybrid_remote"})

    assert config.memory_backend == "memory"


def test_hybrid_remote_memory_backend_can_be_enabled_by_remote_flag() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_MEMORY_BACKEND": "hybrid_remote",
            "MULTIMODAL_AGENT_MEMORY_REMOTE_ENABLED": "true",
            "MEMORY_SERVER_BASE_URL": "http://memory.local",
            "MEMORY_SERVER_TIMEOUT_SECONDS": "1.5",
            "MEMORY_SERVER_QUERY_STRATEGY": "hybrid",
            "MEMORY_SERVER_DIRECT_ANSWER": "true",
            "MEMORY_SERVER_INCLUDE_MEDIA_CHUNKS": "true",
        }
    )

    assert config.memory_backend == "hybrid_remote"
    assert config.memory_server_base_url == "http://memory.local"
    assert config.memory_server_timeout_seconds == 1.5
    assert config.memory_server_query_strategy == "hybrid"
    assert config.memory_server_direct_answer is True
    assert config.memory_server_include_media_chunks is True


def test_remote_service_memory_backend_requires_explicit_remote_opt_in() -> None:
    config = ProviderConfig.from_env({"MULTIMODAL_AGENT_MEMORY_BACKEND": "remote_service"})

    assert config.memory_backend == "memory"


def test_remote_service_memory_backend_can_be_enabled_by_remote_flag() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_MEMORY_BACKEND": "remote_service",
            "MULTIMODAL_AGENT_MEMORY_REMOTE_ENABLED": "true",
            "MEMORY_SERVER_BASE_URL": "http://memory.local",
        }
    )

    assert config.memory_backend == "remote_service"
    assert config.memory_server_base_url == "http://memory.local"


def test_create_hybrid_remote_memory_store_wraps_local_jsonl_store(tmp_path) -> None:
    path = tmp_path / "hybrid_local.jsonl"
    store = create_memory_store(
        ProviderConfig(
            memory_backend="hybrid_remote",
            memory_path=str(path),
            memory_server_base_url="http://memory.local",
        )
    )

    assert isinstance(store, HybridMemoryStore)
    saved = store.save(
        MemoryItem(
            memory_id="m1",
            user_id="u1",
            session_id="s1",
            memory_type="task",
            summary="hybrid local write",
            created_at=NOW,
        )
    )

    assert path.exists()
    assert JsonlMemoryStore(path).get("u1", saved.memory_id) is not None


def test_create_remote_service_store_uses_unavailable_adapter_without_local_fallback(tmp_path) -> None:
    path = tmp_path / "remote_service_should_not_be_written.jsonl"
    store = create_memory_store(
        ProviderConfig(
            memory_backend="remote_service",
            memory_path=str(path),
            memory_server_base_url="http://memory.local",
        )
    )

    assert isinstance(store, RemoteServiceMemoryStore)
    with pytest.raises(MemoryServiceOperationError) as exc_info:
        store.save(
            MemoryItem(
                memory_id="m1",
                user_id="u1",
                session_id="s1",
                memory_type="task",
                summary="remote service write",
                created_at=NOW,
            )
        )
    assert exc_info.value.recoverable is True
    assert not path.exists()


def test_jsonl_memory_backend_writes_memory_file_when_auto_promotion_allowed(tmp_path) -> None:
    path = tmp_path / "memories.jsonl"
    runtime = runtime_with_auto_memory(
        config=ProviderConfig(memory_backend="jsonl", memory_path=str(path))
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="找相似款"))

    assert state.status == "completed"
    assert path.exists()
    assert path.read_text(encoding="utf-8").strip()


def test_sqlite_memory_backend_writes_memory_file_when_auto_promotion_allowed(tmp_path) -> None:
    path = tmp_path / "memories.sqlite3"
    runtime = runtime_with_auto_memory(
        config=ProviderConfig(memory_backend="sqlite", memory_path=str(path))
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="找相似款"))

    assert state.status == "completed"
    assert path.exists()
    assert SQLiteMemoryStore(path).list_by_user("u1")


def test_jsonl_memory_backend_resolves_relative_path_from_repo_root(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(memory_factory, "REPO_ROOT", tmp_path)
    store = create_memory_store(ProviderConfig(memory_backend="jsonl", memory_path="relative/memories.jsonl"))

    store.save(
        MemoryItem(
            memory_id="m1",
            user_id="u1",
            session_id="s1",
            memory_type="task",
            summary="relative path memory",
            created_at=NOW,
        )
    )

    assert (tmp_path / "relative" / "memories.jsonl").exists()


def test_sqlite_memory_backend_resolves_relative_path_from_repo_root(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(memory_factory, "REPO_ROOT", tmp_path)
    store = create_memory_store(ProviderConfig(memory_backend="sqlite", memory_path="relative/memories.sqlite3"))

    store.save(
        MemoryItem(
            memory_id="m1",
            user_id="u1",
            session_id="s1",
            memory_type="task",
            summary="relative path memory",
            created_at=NOW,
        )
    )

    assert (tmp_path / "relative" / "memories.sqlite3").exists()


def test_sqlite_memory_backend_default_path_uses_sqlite_extension(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(memory_factory, "REPO_ROOT", tmp_path)
    store = create_memory_store(ProviderConfig(memory_backend="sqlite"))

    store.save(
        MemoryItem(
            memory_id="m1",
            user_id="u1",
            session_id="s1",
            memory_type="task",
            summary="default sqlite path memory",
            created_at=NOW,
        )
    )

    assert (tmp_path / ".local" / "memory" / "long_term_memories.sqlite3").exists()
    assert not (tmp_path / ".local" / "memory" / "long_term_memories.jsonl").exists()


def test_new_runtime_instance_reads_existing_jsonl_memory(tmp_path) -> None:
    path = tmp_path / "memories.jsonl"
    config = ProviderConfig(memory_backend="jsonl", memory_path=str(path))
    runtime_with_auto_memory(config=config).run_state(
        UserRequest(user_id="u1", session_id="s1", text="找相似款")
    )

    state = AgentGraphRuntime(config=config).run_state(
        UserRequest(user_id="u1", session_id="s2", text="继续推荐")
    )

    assert state.memory_context
    assert state.response is not None
    assert state.response.data["memory_context_count"] >= 1


def test_new_runtime_instance_reads_existing_sqlite_memory(tmp_path) -> None:
    path = tmp_path / "memories.sqlite3"
    config = ProviderConfig(memory_backend="sqlite", memory_path=str(path))
    runtime_with_auto_memory(config=config).run_state(
        UserRequest(user_id="u1", session_id="s1", text="找相似款")
    )

    state = AgentGraphRuntime(config=config).run_state(
        UserRequest(user_id="u1", session_id="s2", text="继续推荐")
    )

    assert state.memory_context
    assert state.response is not None
    assert state.response.data["memory_context_count"] >= 1


def test_agent_response_uses_injected_memory_context(tmp_path) -> None:
    path = tmp_path / "memories.jsonl"
    store = JsonlMemoryStore(path)
    runtime_with_auto_memory(memory_store=store).run_state(
        UserRequest(user_id="u1", session_id="s1", text="找相似款")
    )

    state = AgentGraphRuntime(memory_store=store).run_state(
        UserRequest(user_id="u1", session_id="s2", text="这个风格怎么样")
    )

    assert state.response is not None
    assert "参考记忆" in state.response.message
    assert state.response.data["memory_context_summaries"]


def test_runtime_injects_hybrid_remote_memory_context_without_network() -> None:
    requests: list[MemoryServerRequest] = []

    def transport(request: MemoryServerRequest) -> dict:
        requests.append(request)
        return {
            "results": [
                {
                    "type": "text",
                    "content": "Remote memory says the user had coffee and toast.",
                    "score": 0.92,
                    "memory_type": "episodic",
                    "source": {
                        "memory_id": "breakfast-remote",
                        "timestamp_start": "2026-04-11T08:00:00+00:00",
                    },
                }
            ]
        }

    store = HybridMemoryStore(
        local_store=InMemoryStore(),
        remote_client=RemoteMemoryClient(base_url="http://memory.local", transport=transport),
    )
    state = AgentGraphRuntime(memory_store=store).run_state(
        UserRequest(user_id="u1", session_id="s1", text="上次早餐吃了什么")
    )

    assert state.memory_context
    assert [item.memory_id for item in state.memory_context] == ["memory_server:breakfast-remote"]
    assert state.request.metadata["memory_context_injected_ids"] == ["memory_server:breakfast-remote"]
    assert "coffee and toast" in state.request.metadata["memory_context_text"]
    assert state.response is not None
    assert "Remote memory says the user had coffee and toast." in state.response.data["memory_context_summaries"]
    assert requests[0].path == "/v1/memories/query"
    assert requests[0].body["user_id"] == "u1"
    assert "session_id" not in requests[0].body
