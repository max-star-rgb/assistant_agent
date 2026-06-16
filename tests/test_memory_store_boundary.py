from datetime import datetime, timezone
from pathlib import Path

import pytest

from multimodal_agent.memory.jsonl_store import JsonlMemoryStore
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


@pytest.mark.parametrize("store_factory", [InMemoryStore, lambda: JsonlMemoryStore(Path("/tmp/unused"))])
def test_store_boundary_save_search_get_delete(store_factory, tmp_path) -> None:
    if store_factory is InMemoryStore:
        store: MemoryStore = store_factory()
    else:
        store = JsonlMemoryStore(tmp_path / "memories.jsonl")

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
