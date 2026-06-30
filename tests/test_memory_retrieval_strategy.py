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
    *,
    tenant_id: str | None = None,
    project_id: str | None = None,
    scope: str | None = None,
) -> MemoryItem:
    return MemoryItem(
        memory_id=memory_id,
        user_id="u1",
        tenant_id=tenant_id,
        project_id=project_id,
        scope=scope,
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


def test_query_without_keyword_hit_does_not_fallback_to_unrelated_memories() -> None:
    store = InMemoryStore()
    store.save(memory_item("task_hello", "task", "你好，我可以帮你调用多模态工具。"))
    store.save(memory_item("task_guard", "task", "工具调用保护触发，已停止继续调用。"))

    results = MemoryRetrievalStrategy(store).retrieve(
        MemoryQuery(user_id="u1", query="玉桂狗", top_k=5)
    )

    assert results == []


def test_contextual_followup_can_use_recent_memory_fallback() -> None:
    store = InMemoryStore()
    store.save(memory_item("task_recent", "task", "上次帮用户找过白色相似款。"))

    results = MemoryRetrievalStrategy(store).retrieve(
        MemoryQuery(user_id="u1", query="继续推荐", top_k=5)
    )

    assert [item.memory_id for item in results] == ["task_recent"]


def test_empty_query_lists_recent_memory_for_audit_browsing() -> None:
    store = InMemoryStore()
    store.save(memory_item("pref", "preference", "用户喜欢日系极简风格。"))
    store.save(memory_item("task", "task", "曾经先搜索商品再比较价格。"))

    results = MemoryRetrievalStrategy(store).retrieve(
        MemoryQuery(user_id="u1", query="", top_k=5)
    )

    assert {item.memory_id for item in results} == {"pref", "task"}


def test_chinese_phrase_retrieval_matches_relevant_fragments_without_global_fallback() -> None:
    store = InMemoryStore()
    store.save(memory_item("pref", "preference", "用户喜欢日系极简风格。"))
    store.save(memory_item("task", "task", "曾经先搜索商品再比较价格。"))
    store.save(memory_item("unrelated", "task", "你好，我可以帮你调用多模态工具。"))

    results = MemoryRetrievalStrategy(store).retrieve(
        MemoryQuery(user_id="u1", query="日系风格商品推荐", top_k=5)
    )

    assert [item.memory_id for item in results] == ["pref", "task"]


def test_memory_retrieval_filters_by_tenant_project_and_scope() -> None:
    store = InMemoryStore()
    store.save(memory_item("global_pref", "preference", "用户喜欢浅色日系风格。"))
    store.save(memory_item("project_a", "task", "项目 A 使用浅色日系风格。", tenant_id="t1", project_id="p1", scope="project"))
    store.save(memory_item("project_b", "task", "项目 B 使用浅色日系风格。", tenant_id="t1", project_id="p2", scope="project"))
    store.save(memory_item("tenant_other", "task", "其他租户使用浅色日系风格。", tenant_id="t2", project_id="p1", scope="project"))
    store.save(memory_item("video", "video", "视频里出现浅色日系风格。", tenant_id="t1", project_id="p1"))

    results = MemoryRetrievalStrategy(store).retrieve(
        MemoryQuery(
            user_id="u1",
            tenant_id="t1",
            project_id="p1",
            query="浅色日系",
            allowed_scopes=["project", "user_profile"],
            top_k=10,
        )
    )

    assert {item.memory_id for item in results} == {"global_pref", "project_a"}


def test_memory_retrieval_excludes_superseded_by_default_and_allows_debug_include() -> None:
    store = InMemoryStore()
    store.save(
        memory_item(
            "style_old",
            "preference",
            "用户喜欢浅色日系风格。",
            {"preference_key": "style", "superseded_by_memory_id": "style_new"},
        )
    )
    store.save(memory_item("style_new", "preference", "用户喜欢深色极简风格。", {"preference_key": "style"}))

    default_results = MemoryRetrievalStrategy(store).retrieve(
        MemoryQuery(user_id="u1", query="风格", top_k=5)
    )
    debug_results = MemoryRetrievalStrategy(store).retrieve(
        MemoryQuery(user_id="u1", query="风格", top_k=5, include_superseded=True)
    )

    assert [item.memory_id for item in default_results] == ["style_new"]
    assert {item.memory_id for item in debug_results} == {"style_old", "style_new"}


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
