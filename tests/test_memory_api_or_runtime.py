from datetime import datetime, timezone

from multimodal_agent.memory.store import InMemoryStore
from multimodal_agent.schemas.memory import MemoryItem, MemoryQuery


def test_runtime_level_memory_save_search_delete_flow() -> None:
    store = InMemoryStore()
    item = MemoryItem(
        memory_id="m1",
        user_id="u1",
        session_id="s1",
        memory_type="preference",
        summary="用户喜欢日系极简浅色背景。",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    store.save(item)
    search_result = store.search(MemoryQuery(user_id="u1", query="日系极简", top_k=5))

    assert [memory.memory_id for memory in search_result.items] == ["m1"]
    assert "日系极简" in search_result.memory_context
    assert store.get("u1", "m1") == item
    assert store.delete("u1", "m1") is True
    assert store.search(MemoryQuery(user_id="u1", query="日系极简", top_k=5)).items == []
