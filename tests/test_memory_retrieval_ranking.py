from datetime import datetime, timezone

from multimodal_agent.memory.retrieval import MemoryRetrievalStrategy
from multimodal_agent.memory.store import InMemoryStore
from multimodal_agent.schemas.memory import MemoryItem, MemoryQuery


def memory_item(
    memory_id: str,
    memory_type: str,
    summary: str,
    *,
    created_at: datetime,
    session_id: str | None = "s1",
    tags: list[str] | None = None,
    artifact_refs: list[str] | None = None,
) -> MemoryItem:
    return MemoryItem(
        memory_id=memory_id,
        user_id="u1",
        session_id=session_id,
        memory_type=memory_type,
        summary=summary,
        content={"summary": summary},
        tags=tags or [],
        artifact_refs=artifact_refs or [],
        created_at=created_at,
    )


def test_retrieval_respects_top_k_and_recency() -> None:
    store = InMemoryStore()
    store.save(memory_item("old", "product", "白色运动鞋", created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    store.save(memory_item("new", "product", "白色运动鞋", created_at=datetime(2026, 1, 2, tzinfo=timezone.utc)))

    results = MemoryRetrievalStrategy(store).retrieve(MemoryQuery(user_id="u1", query="白色运动鞋", top_k=1))

    assert [item.memory_id for item in results] == ["new"]


def test_retrieval_filters_type_tag_and_session() -> None:
    store = InMemoryStore()
    created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.save(memory_item("m1", "preference", "用户喜欢日系风格", created_at=created_at, tags=["style"]))
    store.save(memory_item("m2", "product", "用户喜欢日系风格商品", created_at=created_at, tags=["style"]))
    store.save(memory_item("m3", "preference", "用户喜欢日系风格", created_at=created_at, session_id="s2", tags=["style"]))

    results = MemoryRetrievalStrategy(store).retrieve(
        MemoryQuery(
            user_id="u1",
            session_id="s1",
            query="日系风格",
            memory_types=["preference"],
            tags=["style"],
            top_k=5,
        )
    )

    assert [item.memory_id for item in results] == ["m1"]


def test_capability_type_priority_prefers_preferences_for_image_generation() -> None:
    store = InMemoryStore()
    created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.save(memory_item("product", "product", "用户喜欢浅色背景", created_at=created_at))
    store.save(memory_item("preference", "preference", "用户喜欢浅色背景", created_at=created_at))

    results = MemoryRetrievalStrategy(store).retrieve(
        MemoryQuery(user_id="u1", query="浅色背景", capability="image_generation", top_k=2)
    )

    assert [item.memory_id for item in results] == ["preference", "product"]


def test_capability_type_priority_uses_product_before_artifact_for_render() -> None:
    store = InMemoryStore()
    created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.save(
        memory_item(
            "artifact",
            "artifact",
            "上次那个白色椅子",
            created_at=created_at,
            artifact_refs=["mock://image/chair"],
        )
    )
    store.save(memory_item("product", "product", "上次那个白色椅子", created_at=created_at))

    results = MemoryRetrievalStrategy(store).retrieve(
        MemoryQuery(user_id="u1", query="白色椅子", capability="render_3d", top_k=2)
    )

    assert [item.memory_id for item in results] == ["product", "artifact"]
