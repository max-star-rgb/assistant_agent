from datetime import datetime, timezone

from multimodal_agent.agent.runtime import AgentGraphRuntime
from multimodal_agent.config import ProviderConfig
from multimodal_agent.memory import factory as memory_factory
from multimodal_agent.memory.factory import create_memory_store
from multimodal_agent.memory.jsonl_store import JsonlMemoryStore
from multimodal_agent.memory.store import InMemoryStore
from multimodal_agent.schemas.memory import MemoryItem
from multimodal_agent.schemas.requests import UserRequest


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_default_memory_backend_is_in_memory() -> None:
    store = create_memory_store(ProviderConfig.from_env({}))

    assert isinstance(store, InMemoryStore)


def test_jsonl_memory_backend_writes_memory_file(tmp_path) -> None:
    path = tmp_path / "memories.jsonl"
    runtime = AgentGraphRuntime(
        config=ProviderConfig(memory_backend="jsonl", memory_path=str(path))
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="找相似款"))

    assert state.status == "completed"
    assert path.exists()
    assert path.read_text(encoding="utf-8").strip()


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


def test_new_runtime_instance_reads_existing_jsonl_memory(tmp_path) -> None:
    path = tmp_path / "memories.jsonl"
    config = ProviderConfig(memory_backend="jsonl", memory_path=str(path))
    AgentGraphRuntime(config=config).run_state(
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
    AgentGraphRuntime(memory_store=store).run_state(
        UserRequest(user_id="u1", session_id="s1", text="找相似款")
    )

    state = AgentGraphRuntime(memory_store=store).run_state(
        UserRequest(user_id="u1", session_id="s2", text="这个风格怎么样")
    )

    assert state.response is not None
    assert "参考记忆" in state.response.message
    assert state.response.data["memory_context_summaries"]
