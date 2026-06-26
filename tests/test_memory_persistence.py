from datetime import datetime, timezone

from multimodal_agent.memory.jsonl_store import JsonlMemoryStore
from multimodal_agent.schemas.memory import MemoryItem


def make_memory(memory_id: str = "m1") -> MemoryItem:
    return MemoryItem(
        memory_id=memory_id,
        user_id="u1",
        memory_type="product",
        content={"name": "黑色包", "style": "日系风格"},
        summary="用户关注过一个黑色包，偏好日系风格。",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def test_jsonl_memory_store_creates_file_after_save(tmp_path) -> None:
    path = tmp_path / "memories.jsonl"
    store = JsonlMemoryStore(path)

    store.save(make_memory())

    assert path.exists()
    assert path.read_text(encoding="utf-8").strip()
    first_line = path.read_text(encoding="utf-8").splitlines()[0]
    assert MemoryItem.model_validate_json(first_line).memory_id == "m1"


def test_jsonl_memory_store_reads_memory_after_recreate(tmp_path) -> None:
    path = tmp_path / "memories.jsonl"
    JsonlMemoryStore(path).save(make_memory())

    reloaded_store = JsonlMemoryStore(path)
    item = reloaded_store.get("u1", "m1")

    assert item is not None
    assert item.memory_id == "m1"
    assert item.summary == "用户关注过一个黑色包，偏好日系风格。"


def test_jsonl_memory_store_search_returns_related_records(tmp_path) -> None:
    path = tmp_path / "memories.jsonl"
    store = JsonlMemoryStore(path)
    store.save(make_memory("m1"))
    store.save(
        MemoryItem(
            memory_id="m2",
            user_id="u2",
            memory_type="product",
            content={"name": "黑色包"},
            summary="另一个用户的黑色包记忆",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )

    results = store.search(user_id="u1", query="黑色包")

    assert [item.memory_id for item in results] == ["m1"]
    assert results[0].relevance is not None
