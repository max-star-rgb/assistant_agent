from datetime import datetime, timezone

from multimodal_agent.memory.retriever import KeywordMemoryRetriever
from multimodal_agent.memory.store import InMemoryStore
from multimodal_agent.schemas.memory import MemoryItem


def memory_item(
    memory_id: str,
    user_id: str,
    memory_type: str,
    summary: str,
    content: dict,
) -> MemoryItem:
    return MemoryItem(
        memory_id=memory_id,
        user_id=user_id,
        memory_type=memory_type,
        content=content,
        summary=summary,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def test_save_and_get_memory() -> None:
    store = InMemoryStore()
    item = memory_item(
        memory_id="m1",
        user_id="u1",
        memory_type="product",
        summary="用户看过一个黑色包",
        content={"name": "黑色包"},
    )

    saved = store.save(item)

    assert saved == item
    assert store.get("u1", "m1") == item


def test_list_by_user_isolated_by_user_id() -> None:
    store = InMemoryStore()
    store.save(
        memory_item(
            memory_id="m1",
            user_id="u1",
            memory_type="product",
            summary="用户看过一个黑色包",
            content={"name": "黑色包"},
        )
    )
    store.save(
        memory_item(
            memory_id="m2",
            user_id="u2",
            memory_type="preference",
            summary="用户喜欢日系风格",
            content={"style": "日系风格"},
        )
    )

    assert [item.memory_id for item in store.list_by_user("u1")] == ["m1"]
    assert [item.memory_id for item in store.list_by_user("u2")] == ["m2"]


def test_retriever_does_not_leak_across_users() -> None:
    store = InMemoryStore()
    store.save(
        memory_item(
            memory_id="m1",
            user_id="u1",
            memory_type="product",
            summary="用户看过一个黑色包",
            content={"name": "黑色包"},
        )
    )
    store.save(
        memory_item(
            memory_id="m2",
            user_id="u2",
            memory_type="product",
            summary="另一个用户也看过黑色包",
            content={"name": "黑色包"},
        )
    )

    results = KeywordMemoryRetriever(store).search("u1", "黑色包")

    assert [item.memory_id for item in results] == ["m1"]


def test_keyword_retriever_matches_black_bag_memory() -> None:
    store = InMemoryStore()
    store.save(
        memory_item(
            memory_id="m1",
            user_id="u1",
            memory_type="product",
            summary="用户上次关注了黑色包",
            content={"product": "黑色包", "color": "黑色"},
        )
    )

    results = KeywordMemoryRetriever(store).search("u1", "黑色包")

    assert len(results) == 1
    assert results[0].memory_id == "m1"
    assert results[0].relevance is not None


def test_keyword_retriever_matches_japanese_style_preference() -> None:
    store = InMemoryStore()
    store.save(
        memory_item(
            memory_id="m1",
            user_id="u1",
            memory_type="preference",
            summary="用户喜欢日系风格",
            content={"style": "日系风格", "tone": "简约"},
        )
    )

    results = KeywordMemoryRetriever(store).search("u1", "日系风格")

    assert [item.memory_id for item in results] == ["m1"]


def test_retriever_can_filter_memory_types() -> None:
    store = InMemoryStore()
    store.save(
        memory_item(
            memory_id="m1",
            user_id="u1",
            memory_type="product",
            summary="黑色包商品",
            content={"name": "黑色包"},
        )
    )
    store.save(
        memory_item(
            memory_id="m2",
            user_id="u1",
            memory_type="preference",
            summary="喜欢黑色包",
            content={"style": "黑色包"},
        )
    )

    results = KeywordMemoryRetriever(store).search("u1", "黑色包", memory_types={"preference"})

    assert [item.memory_id for item in results] == ["m2"]


def test_empty_query_returns_no_results() -> None:
    store = InMemoryStore()
    store.save(
        memory_item(
            memory_id="m1",
            user_id="u1",
            memory_type="product",
            summary="黑色包商品",
            content={"name": "黑色包"},
        )
    )

    assert KeywordMemoryRetriever(store).search("u1", "") == []


def test_clear_user_removes_only_that_users_memories() -> None:
    store = InMemoryStore()
    store.save(
        memory_item(
            memory_id="m1",
            user_id="u1",
            memory_type="product",
            summary="黑色包商品",
            content={"name": "黑色包"},
        )
    )
    store.save(
        memory_item(
            memory_id="m2",
            user_id="u2",
            memory_type="product",
            summary="黑色包商品",
            content={"name": "黑色包"},
        )
    )

    store.clear_user("u1")

    assert store.list_by_user("u1") == []
    assert [item.memory_id for item in store.list_by_user("u2")] == ["m2"]
