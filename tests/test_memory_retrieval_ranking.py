from datetime import datetime, timezone

from assistant_agent.memory.retrieval import MemoryRetrievalStrategy
from assistant_agent.memory.sqlite_store import SQLiteMemoryStore
from assistant_agent.memory.store import InMemoryStore
from assistant_agent.schemas.memory import MemoryItem, MemoryQuery
from assistant_agent.schemas.memory_intelligence import MemoryFact


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


def test_retriever_uses_store_native_candidate_search_when_available() -> None:
    class CandidateStore(InMemoryStore):
        def __init__(self) -> None:
            super().__init__()
            self.calls: list[tuple[str, str, int, set[str] | None]] = []

        def search_candidates(self, *, user_id, query, limit, memory_types=None):
            self.calls.append((user_id, query, limit, memory_types))
            return [
                memory_item(
                    "native",
                    "product",
                    "本地候选",
                    created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                ).model_copy(update={"relevance": 0.8})
            ]

        def list_by_user(self, user_id: str):
            raise AssertionError("native candidate search should avoid a full store scan")

    store = CandidateStore()

    results = MemoryRetrievalStrategy(store).retrieve(
        MemoryQuery(user_id="u1", query="本地候选", memory_types=["product"], top_k=2)
    )

    assert [item.memory_id for item in results] == ["native"]
    assert store.calls == [("u1", "本地候选", 8, {"product"})]


def test_sqlite_and_memory_backends_recall_same_chinese_phrase(tmp_path) -> None:
    memory_store = InMemoryStore()
    sqlite_store = SQLiteMemoryStore(tmp_path / "memories.sqlite3", synchronous="OFF")
    for store in (memory_store, sqlite_store):
        store.save(
            memory_item(
                "style",
                "preference",
                "用户偏好深色极简海报",
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        )

    query = MemoryQuery(user_id="u1", query="深色极简", top_k=5)

    assert [item.memory_id for item in MemoryRetrievalStrategy(memory_store).retrieve(query)] == ["style"]
    assert [item.memory_id for item in MemoryRetrievalStrategy(sqlite_store).retrieve(query)] == ["style"]


def test_exact_structured_fact_value_outranks_loose_summary_overlap() -> None:
    store = InMemoryStore()
    fact = MemoryFact(
        fact_key="user:preference:theme",
        subject="user",
        predicate="preference.theme",
        value="dark",
        provenance="user_explicit",
        conflict_policy="replace",
        observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    store.save(
        MemoryItem(
            memory_id="fact_exact",
            user_id="u1",
            memory_type="preference",
            summary="主题偏好",
            content={"fact": fact.model_dump(mode="json")},
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )
    store.save(
        memory_item(
            "summary_loose",
            "preference",
            "dark suggestion",
            created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )
    )

    results = MemoryRetrievalStrategy(store).retrieve(MemoryQuery(user_id="u1", query="dark", top_k=2))

    assert [item.memory_id for item in results] == ["fact_exact", "summary_loose"]
    assert results[0].relevance == 1.0


def test_local_store_search_reports_backend_neutral_ranking_reason(tmp_path) -> None:
    memory_store = InMemoryStore()
    sqlite_store = SQLiteMemoryStore(tmp_path / "memories.sqlite3", synchronous="OFF")
    item = memory_item(
        "m1", "product", "深色海报", created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    memory_store.save(item)
    sqlite_store.save(item)

    assert memory_store.search(MemoryQuery(user_id="u1", query="深色")).ranking_reason == (
        "local_text_match_type_priority_recency"
    )
    assert sqlite_store.search(MemoryQuery(user_id="u1", query="深色")).ranking_reason == (
        "local_text_match_type_priority_recency"
    )
