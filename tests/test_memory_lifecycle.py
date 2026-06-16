from datetime import datetime, timezone

from multimodal_agent.memory.retrieval import MemoryRetrievalStrategy
from multimodal_agent.memory.store import InMemoryStore
from multimodal_agent.memory.write_policy import MemoryWritePolicy, build_task_summary_memory_item
from multimodal_agent.schemas.memory import MemoryItem, MemoryQuery


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_policy_assigns_expires_at_for_task_memory() -> None:
    item = build_task_summary_memory_item(
        memory_id="m1",
        user_id="u1",
        session_id="s1",
        summary="已完成任务。",
        intent="direct_chat",
        selected_tools=[],
        created_at=NOW,
    )

    assert item is not None
    assert item.expires_at is not None
    assert item.expires_at > item.created_at


def test_policy_keeps_preference_memory_long_lived() -> None:
    policy = MemoryWritePolicy()

    assert policy.expires_at_for("preference", NOW) is None


def test_retrieval_excludes_expired_memory_by_default() -> None:
    store = InMemoryStore()
    store.save(
        MemoryItem(
            memory_id="expired",
            user_id="u1",
            memory_type="task",
            summary="过期记忆",
            created_at=NOW,
            expires_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
    )

    results = MemoryRetrievalStrategy(store).retrieve(MemoryQuery(user_id="u1", query="过期", top_k=5))

    assert results == []


def test_retrieval_can_include_expired_memory_when_requested() -> None:
    store = InMemoryStore()
    store.save(
        MemoryItem(
            memory_id="expired",
            user_id="u1",
            memory_type="task",
            summary="过期记忆",
            created_at=NOW,
            expires_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
    )

    results = MemoryRetrievalStrategy(store).retrieve(
        MemoryQuery(user_id="u1", query="过期", include_expired=True, top_k=5)
    )

    assert [item.memory_id for item in results] == ["expired"]
