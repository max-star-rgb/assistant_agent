from datetime import datetime, timezone

import pytest

from multimodal_agent.memory.jsonl_store import JsonlMemoryStore
from multimodal_agent.memory.sqlite_store import SQLiteMemoryStore
from multimodal_agent.memory.store import InMemoryStore, MemoryStore
from multimodal_agent.schemas.memory import MemoryItem, MemoryQuery, MemorySearchResult


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_memory(memory_id: str, user_id: str = "u1", summary: str = "用户喜欢白色运动鞋") -> MemoryItem:
    return MemoryItem(
        memory_id=memory_id,
        user_id=user_id,
        session_id="s1",
        memory_type="product",
        summary=summary,
        content={"name": "白色运动鞋", "output_ref": "mock://product/shoe-1"},
        tags=["shoe"],
        artifact_refs=["mock://product/shoe-1"],
        created_at=NOW,
    )


@pytest.mark.parametrize("store_backend", ["memory", "jsonl", "sqlite"])
def test_store_boundary_save_search_get_delete(store_backend: str, tmp_path) -> None:
    if store_backend == "memory":
        store: MemoryStore = InMemoryStore()
    elif store_backend == "jsonl":
        store = JsonlMemoryStore(tmp_path / "memories.jsonl")
    else:
        store = SQLiteMemoryStore(tmp_path / "memories.sqlite3")

    saved = store.save(make_memory("m1"))
    store.save(make_memory("m2", user_id="u2", summary="另一个用户喜欢白色运动鞋"))

    result = store.search(MemoryQuery(user_id="u1", query="白色运动鞋", tags=["shoe"], top_k=5))

    assert isinstance(result, MemorySearchResult)
    assert saved == store.get("u1", "m1")
    assert [item.memory_id for item in result.items] == ["m1"]
    assert result.total == 1
    assert "白色运动鞋" in result.memory_context
    assert store.delete("u1", "m1") is True
    assert store.get("u1", "m1") is None
    assert store.delete("u1", "missing") is False


def test_jsonl_store_keeps_legacy_search_call_compatible(tmp_path) -> None:
    store = JsonlMemoryStore(tmp_path / "memories.jsonl")
    store.save(make_memory("m1"))

    result = store.search(user_id="u1", query="白色运动鞋")

    assert [item.memory_id for item in result] == ["m1"]


def test_sqlite_store_persists_across_instances(tmp_path) -> None:
    path = tmp_path / "memories.sqlite3"
    SQLiteMemoryStore(path).save(make_memory("m1"))

    result = SQLiteMemoryStore(path).search(MemoryQuery(user_id="u1", query="白色运动鞋"))

    assert [item.memory_id for item in result.items] == ["m1"]


def test_sqlite_store_delete_by_session_and_clear_user(tmp_path) -> None:
    store = SQLiteMemoryStore(tmp_path / "memories.sqlite3")
    store.save(make_memory("m1"))
    store.save(make_memory("m2", summary="用户喜欢黑色运动鞋"))
    store.save(make_memory("m3", user_id="u2"))

    assert store.delete_by_session("u1", "s1") == 2
    assert store.list_by_user("u1") == []
    assert [item.memory_id for item in store.list_by_user("u2")] == ["m3"]

    store.clear_user("u2")

    assert store.list_by_user("u2") == []
