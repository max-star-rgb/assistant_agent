from datetime import datetime, timezone

import pytest

from multimodal_agent.memory.jsonl_store import JsonlMemoryStore
from multimodal_agent.memory.store import InMemoryStore
from multimodal_agent.schemas.memory import MemoryItem


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def memory_item(memory_id: str, user_id: str, session_id: str = "s1") -> MemoryItem:
    return MemoryItem(
        memory_id=memory_id,
        user_id=user_id,
        session_id=session_id,
        memory_type="task",
        summary=f"memory {memory_id}",
        created_at=NOW,
    )


@pytest.mark.parametrize("store_factory", [InMemoryStore, "jsonl"])
def test_delete_by_memory_id_and_user_does_not_cross_users(store_factory, tmp_path) -> None:
    store = InMemoryStore() if store_factory is InMemoryStore else JsonlMemoryStore(tmp_path / "memories.jsonl")
    store.save(memory_item("shared", "u1"))
    store.save(memory_item("shared", "u2"))

    assert store.delete("u1", "shared") is True
    assert store.get("u1", "shared") is None
    assert store.get("u2", "shared") is not None


@pytest.mark.parametrize("store_factory", [InMemoryStore, "jsonl"])
def test_delete_by_session_is_user_scoped(store_factory, tmp_path) -> None:
    store = InMemoryStore() if store_factory is InMemoryStore else JsonlMemoryStore(tmp_path / "memories.jsonl")
    store.save(memory_item("m1", "u1", "s1"))
    store.save(memory_item("m2", "u1", "s2"))
    store.save(memory_item("m3", "u2", "s1"))

    assert store.delete_by_session("u1", "s1") == 1
    assert [item.memory_id for item in store.list_by_user("u1")] == ["m2"]
    assert [item.memory_id for item in store.list_by_user("u2")] == ["m3"]
