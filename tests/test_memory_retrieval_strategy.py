from datetime import datetime, timezone

from multimodal_agent.agent.runtime import AgentGraphRuntime
from multimodal_agent.config import ProviderConfig
from multimodal_agent.memory.jsonl_store import JsonlMemoryStore
from multimodal_agent.memory.retrieval import MemoryRetrievalStrategy, format_memory_context
from multimodal_agent.memory.store import InMemoryStore
from multimodal_agent.schemas.memory import MemoryItem, MemoryQuery
from multimodal_agent.schemas.requests import UserRequest


def memory_item(
    memory_id: str,
    memory_type: str,
    summary: str,
    content: dict | None = None,
) -> MemoryItem:
    return MemoryItem(
        memory_id=memory_id,
        user_id="u1",
        memory_type=memory_type,
        summary=summary,
        content=content or {},
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def test_memory_retrieval_filters_by_type() -> None:
    store = InMemoryStore()
    store.save(memory_item("m1", "product", "用户关注白色低帮运动鞋"))
    store.save(memory_item("m2", "preference", "用户喜欢日系白色风格"))

    results = MemoryRetrievalStrategy(store).retrieve(
        MemoryQuery(user_id="u1", query="白色", memory_types=["preference"], top_k=5)
    )

    assert [item.memory_id for item in results] == ["m2"]


def test_memory_retrieval_respects_top_k() -> None:
    store = InMemoryStore()
    for index in range(3):
        store.save(memory_item(f"m{index}", "product", f"白色鞋子记忆 {index}"))

    results = MemoryRetrievalStrategy(store).retrieve(
        MemoryQuery(user_id="u1", query="白色鞋子", top_k=2)
    )

    assert len(results) == 2


def test_jsonl_memory_retrieval_works_across_instances(tmp_path) -> None:
    path = tmp_path / "memories.jsonl"
    JsonlMemoryStore(path).save(memory_item("m1", "product", "用户关注黑色包"))

    reloaded = JsonlMemoryStore(path)
    results = MemoryRetrievalStrategy(reloaded).retrieve(
        MemoryQuery(user_id="u1", query="黑色包", top_k=3)
    )

    assert [item.memory_id for item in results] == ["m1"]


def test_memory_context_formatter_respects_character_limit() -> None:
    items = [
        memory_item("m1", "preference", "用户喜欢日系极简浅色背景" * 10),
        memory_item("m2", "product", "用户关注白色低帮运动鞋" * 10),
    ]

    context = format_memory_context(items, max_chars=80)

    assert len(context) <= 80
    assert context.startswith("相关历史：")


def test_runtime_response_contains_formatted_memory_context(tmp_path) -> None:
    path = tmp_path / "memories.jsonl"
    config = ProviderConfig(memory_backend="jsonl", memory_path=str(path))
    store = JsonlMemoryStore(path)
    store.save(memory_item("m1", "preference", "用户喜欢日系风格", {"session_id": "s1"}))

    state = AgentGraphRuntime(config=config).run_state(
        UserRequest(user_id="u1", session_id="s2", text="日系风格推荐")
    )

    assert state.response is not None
    assert "相关历史" in state.response.data["memory_context_text"]
    assert state.response.data["memory_context_count"] == 1
