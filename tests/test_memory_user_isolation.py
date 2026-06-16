from datetime import datetime, timezone

from multimodal_agent.memory.store import InMemoryStore
from multimodal_agent.schemas.memory import MemoryItem, MemoryQuery


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def memory_item(memory_id: str, user_id: str, summary: str) -> MemoryItem:
    return MemoryItem(
        memory_id=memory_id,
        user_id=user_id,
        memory_type="product",
        summary=summary,
        created_at=NOW,
    )


def test_user_a_cannot_search_user_b_memory() -> None:
    store = InMemoryStore()
    store.save(memory_item("a", "user_a", "用户 A 的白色鞋子"))
    store.save(memory_item("b", "user_b", "用户 B 的白色鞋子"))

    result = store.search(MemoryQuery(user_id="user_a", query="白色鞋子"))

    assert [item.memory_id for item in result.items] == ["a"]
    assert "用户 B" not in result.memory_context


def test_user_a_cannot_delete_user_b_memory() -> None:
    store = InMemoryStore()
    store.save(memory_item("same", "user_a", "用户 A 的记忆"))
    store.save(memory_item("same", "user_b", "用户 B 的记忆"))

    assert store.delete("user_a", "same") is True
    assert store.get("user_a", "same") is None
    assert store.get("user_b", "same") is not None
