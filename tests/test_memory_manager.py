from datetime import datetime, timezone

from multimodal_agent.agent.state import AgentState
from multimodal_agent.memory.manager import MemoryManager
from multimodal_agent.memory.profile import USER_PROFILE_MEMORY_ID
from multimodal_agent.memory.store import InMemoryStore
from multimodal_agent.memory.write_policy import MemoryWritePolicy
from multimodal_agent.schemas.identity import RequestIdentity
from multimodal_agent.schemas.memory import MemoryItem, MemoryQuery
from multimodal_agent.schemas.requests import AgentResponse, UserRequest
from multimodal_agent.schemas.tools import ToolResult
from multimodal_agent.tools.base import ToolContext
from multimodal_agent.tools.memory_tool import MemoryRetrievalTool, MemorySaveTool


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def memory_item(memory_id: str, memory_type: str, summary: str) -> MemoryItem:
    return MemoryItem(
        memory_id=memory_id,
        user_id="u1",
        session_id="s1",
        memory_type=memory_type,
        summary=summary,
        created_at=NOW,
    )


def test_memory_manager_loads_layered_context_into_state() -> None:
    store = InMemoryStore()
    store.save(memory_item("pref", "preference", "用户喜欢日系极简风格。"))
    store.save(memory_item("task", "task", "曾经先搜索商品再比较价格。"))
    manager = MemoryManager(store)
    request = UserRequest(user_id="u1", session_id="s2", text="日系风格商品推荐")
    state = AgentState.from_request(request)

    context = manager.load_into_state(state, request)

    assert [item.memory_id for item in context.items] == ["pref", "task"]
    assert state.memory_context == context.items
    assert "偏好/事实记忆" in state.request.metadata["memory_context_text"]
    assert "任务/经历记忆" in state.request.metadata["memory_context_text"]
    assert state.request.metadata["memory_context_summaries"] == [
        "用户喜欢日系极简风格。",
        "曾经先搜索商品再比较价格。",
    ]


def test_memory_manager_records_promotion_candidate_without_default_write() -> None:
    store = InMemoryStore()
    manager = MemoryManager(store)
    request = UserRequest(user_id="u1", session_id="s1", text="帮我找白鞋")
    state = AgentState.from_request(request)
    state.set_response(AgentResponse(message="已完成商品搜索。"))

    item = manager.save_from_run(state)

    assert item is None
    assert store.search(MemoryQuery(user_id="u1", query="商品搜索")).items == []
    assert state.request.metadata["memory_promotion_candidates"] == 1
    assert state.request.metadata.get("memory_promotion_written", 0) == 0
    assert state.request.metadata["memory_promotion_rejected"] == 1
    audit = state.request.metadata["memory_promotion_candidate_audit"]
    assert audit[0]["allowed"] is False
    assert "content" not in audit[0]


def test_memory_manager_search_for_identity_overrides_query_user_id() -> None:
    store = InMemoryStore()
    store.save(memory_item("u1_memory", "preference", "用户喜欢浅色日系风格。"))
    store.save(
        MemoryItem(
            memory_id="u2_memory",
            user_id="u2",
            session_id="s1",
            memory_type="preference",
            summary="另一个用户喜欢浅色日系风格。",
            created_at=NOW,
        )
    )
    manager = MemoryManager(store)

    result = manager.search_for_identity(
        RequestIdentity.for_user(user_id="u1"),
        MemoryQuery(user_id="u2", query="浅色日系"),
    )

    assert [item.memory_id for item in result.items] == ["u1_memory"]


def test_memory_manager_save_explicit_for_identity_ignores_external_user_id() -> None:
    store = InMemoryStore()
    manager = MemoryManager(store)
    identity = RequestIdentity.for_user(user_id="u1", session_id="trusted_session")

    saved = manager.save_explicit_for_identity(
        identity,
        memory_id="m1",
        text="记住我喜欢浅色日系海报",
        content={"user_id": "u2", "summary": "记住我喜欢浅色日系海报"},
        created_at=NOW,
    )

    assert saved.user_id == "u1"
    assert saved.session_id == "trusted_session"
    assert store.list_by_user("u2") == []
    assert store.get("u1", "m1") is not None


def test_memory_manager_identity_filters_project_tenant_and_scope() -> None:
    store = InMemoryStore()
    store.save(memory_item("global_pref", "preference", "用户喜欢浅色日系风格。"))
    store.save(
        MemoryItem(
            memory_id="project_a",
            user_id="u1",
            tenant_id="t1",
            project_id="p1",
            session_id="s1",
            scope="project",
            memory_type="task",
            summary="项目 A 使用浅色日系风格。",
            created_at=NOW,
        )
    )
    store.save(
        MemoryItem(
            memory_id="project_b",
            user_id="u1",
            tenant_id="t1",
            project_id="p2",
            session_id="s1",
            scope="project",
            memory_type="task",
            summary="项目 B 使用浅色日系风格。",
            created_at=NOW,
        )
    )
    store.save(
        MemoryItem(
            memory_id="tenant_other",
            user_id="u1",
            tenant_id="t2",
            project_id="p1",
            session_id="s1",
            scope="project",
            memory_type="task",
            summary="其他租户使用浅色日系风格。",
            created_at=NOW,
        )
    )
    store.save(
        MemoryItem(
            memory_id="video",
            user_id="u1",
            tenant_id="t1",
            project_id="p1",
            session_id="s1",
            memory_type="video",
            summary="视频里出现浅色日系风格。",
            created_at=NOW,
        )
    )
    manager = MemoryManager(store)
    identity = RequestIdentity.for_user(
        tenant_id="t1",
        user_id="u1",
        project_id="p1",
        allowed_scopes=["project", "user_profile"],
    )

    result = manager.search_for_identity(identity, MemoryQuery(user_id="u1", query="浅色日系", top_k=10))

    assert {item.memory_id for item in result.items} == {"global_pref", "project_a"}
    assert {item.memory_id for item in manager.list_for_identity(identity)} == {"global_pref", "project_a"}
    assert manager.get_for_identity(identity, "project_b") is None
    assert manager.delete_for_identity(identity, "project_b") is False
    assert store.get("u1", "project_b") is not None


def test_memory_manager_identity_delete_session_only_deletes_visible_items() -> None:
    store = InMemoryStore()
    store.save(
        MemoryItem(
            memory_id="project_a",
            user_id="u1",
            tenant_id="t1",
            project_id="p1",
            session_id="s1",
            scope="project",
            memory_type="task",
            summary="项目 A 任务。",
            created_at=NOW,
        )
    )
    store.save(
        MemoryItem(
            memory_id="project_b",
            user_id="u1",
            tenant_id="t1",
            project_id="p2",
            session_id="s1",
            scope="project",
            memory_type="task",
            summary="项目 B 任务。",
            created_at=NOW,
        )
    )
    manager = MemoryManager(store)
    identity = RequestIdentity.for_user(
        tenant_id="t1",
        user_id="u1",
        project_id="p1",
        session_id="s1",
        allowed_scopes=["project"],
    )

    deleted = manager.delete_session_for_identity(identity)

    assert deleted == 1
    assert store.get("u1", "project_a") is None
    assert store.get("u1", "project_b") is not None


def test_memory_manager_save_explicit_for_identity_writes_project_fields() -> None:
    store = InMemoryStore()
    manager = MemoryManager(store)
    identity = RequestIdentity.for_user(
        tenant_id="t1",
        user_id="u1",
        project_id="p1",
        session_id="s1",
    )

    saved = manager.save_explicit_for_identity(
        identity,
        memory_id="m1",
        text="记住这个项目使用浅色日系风格",
        scope="project",
        created_at=NOW,
    )

    assert saved.tenant_id == "t1"
    assert saved.project_id == "p1"
    assert saved.scope == "project"
    assert store.get("u1", USER_PROFILE_MEMORY_ID) is None


def test_memory_manager_does_not_merge_duplicates_across_projects() -> None:
    store = InMemoryStore()
    manager = MemoryManager(store)

    first = manager.save_explicit_for_identity(
        RequestIdentity.for_user(
            tenant_id="t1",
            user_id="u1",
            project_id="p1",
            session_id="s1",
        ),
        memory_id="project_1_memory",
        text="记住这个项目使用浅色日系风格",
        scope="project",
        created_at=NOW,
    )
    second = manager.save_explicit_for_identity(
        RequestIdentity.for_user(
            tenant_id="t1",
            user_id="u1",
            project_id="p2",
            session_id="s1",
        ),
        memory_id="project_2_memory",
        text="记住这个项目使用浅色日系风格",
        scope="project",
        created_at=NOW,
    )

    assert first.memory_id == "project_1_memory"
    assert second.memory_id == "project_2_memory"
    assert {item.memory_id for item in store.list_by_user("u1")} == {"project_1_memory", "project_2_memory"}


def test_memory_manager_writes_completed_run_summary_when_policy_allows() -> None:
    store = InMemoryStore()
    manager = MemoryManager(store, write_policy=MemoryWritePolicy(allow_auto_write=True))
    request = UserRequest(user_id="u1", session_id="s1", text="帮我找白鞋")
    state = AgentState.from_request(request)
    state.set_response(AgentResponse(message="已完成商品搜索。"))

    item = manager.save_from_run(state)

    assert item is not None
    assert item.memory_type == "task"
    assert item.source == "agent_run_summary_candidate"
    assert store.search(MemoryQuery(user_id="u1", query="商品搜索")).items
    assert state.request.metadata["memory_promotion_candidates"] == 1
    assert state.request.metadata["memory_promotion_written"] == 1


def test_memory_manager_skips_task_summary_for_pure_memory_save_run() -> None:
    store = InMemoryStore()
    manager = MemoryManager(store)
    request = UserRequest(user_id="u1", session_id="s1", text="记住我爱玉桂狗")
    state = AgentState.from_request(request)
    state.set_response(AgentResponse(message="已记住。"))
    state.tool_results.append(
        ToolResult(
            tool_name="memory_save",
            success=True,
            data={"memory_id": "m1"},
            output_ref="local://memory/m1",
        )
    )

    item = manager.save_from_run(state)

    assert item is None
    assert store.list_by_user("u1") == []


def test_memory_save_tool_uses_manager_store_when_available() -> None:
    store = InMemoryStore()
    manager = MemoryManager(store)

    result = MemorySaveTool().run(
        {
            "user_id": "u1",
            "session_id": "s1",
            "content": {"summary": "用户喜欢浅色日系海报。", "style": "日系"},
        },
        ToolContext(user_id="u1", session_id="s1", metadata={"memory_manager": manager}),
    )

    assert result.success is True
    assert result.data is not None
    assert result.data["summary"] == "已保存用户偏好。"
    assert store.search(MemoryQuery(user_id="u1", query="浅色日系")).items


def test_memory_save_tool_accepts_legacy_action_field() -> None:
    store = InMemoryStore()
    manager = MemoryManager(store)

    result = MemorySaveTool().run(
        {
            "action": "save",
            "user_id": "u1",
            "session_id": "s1",
            "content": {"summary": "用户喜欢浅色日系海报。", "style": "日系"},
        },
        ToolContext(user_id="u1", session_id="s1", metadata={"memory_manager": manager}),
    )

    assert result.success is True
    assert store.search(MemoryQuery(user_id="u1", query="浅色日系")).items


def test_memory_save_tool_accepts_query_as_explicit_text() -> None:
    store = InMemoryStore()
    manager = MemoryManager(store)

    result = MemorySaveTool().run(
        {"user_id": "u1", "session_id": "s1", "query": "记住我喜欢黑色通勤包"},
        ToolContext(user_id="u1", session_id="s1", metadata={"memory_manager": manager}),
    )

    assert result.success is True
    assert store.search(MemoryQuery(user_id="u1", query="黑色通勤包")).items


def test_memory_save_tool_uses_context_identity_over_model_input() -> None:
    store = InMemoryStore()
    manager = MemoryManager(store)

    result = MemorySaveTool().run(
        {"user_id": "user_default", "session_id": "model_session", "query": "记住我喜欢黄瓜味薯片"},
        ToolContext(user_id="00test", session_id="web_session", metadata={"memory_manager": manager}),
    )

    assert result.success is True
    assert store.list_by_user("user_default") == []
    saved = store.search(MemoryQuery(user_id="00test", query="黄瓜味薯片")).items
    assert saved
    assert {item.session_id for item in saved} == {"web_session"}


def test_memory_save_tool_rejects_missing_explicit_text() -> None:
    store = InMemoryStore()
    manager = MemoryManager(store)

    result = MemorySaveTool().run(
        {
            "action": "save",
            "user_id": "u1",
            "session_id": "s1",
            "content": {"style": "日系"},
        },
        ToolContext(user_id="u1", session_id="s1", metadata={"memory_manager": manager}),
    )

    assert result.success is False
    assert result.error == "缺少保存内容，无法写入记忆"
    assert store.list_by_user("u1") == []


def test_memory_manager_merges_duplicate_explicit_memories() -> None:
    store = InMemoryStore()
    manager = MemoryManager(store)

    first = manager.save_explicit(
        user_id="u1",
        session_id="s1",
        text="记住我喜欢日系极简风格",
        content={"style": "日系极简"},
        created_at=NOW,
    )
    second = manager.save_explicit(
        user_id="u1",
        session_id="s1",
        text="记住：我喜欢日系极简风格。",
        content={"style": "日系极简"},
        created_at=NOW,
    )

    explicit_items = [
        item
        for item in store.list_by_user("u1")
        if item.source == "explicit_user_request"
    ]
    assert second.memory_id == first.memory_id
    assert len(explicit_items) == 1
    assert explicit_items[0].content["observation_count"] == 2


def test_memory_manager_updates_user_profile_from_preference() -> None:
    store = InMemoryStore()
    manager = MemoryManager(store)

    saved = manager.save_explicit(
        user_id="u1",
        session_id="s1",
        text="记住我喜欢浅色日系海报",
        content={"style": "浅色日系"},
        created_at=NOW,
    )

    profile = store.get("u1", USER_PROFILE_MEMORY_ID)
    assert profile is not None
    assert profile.source == "user_profile"
    assert profile.memory_type == "preference"
    assert saved.memory_id in profile.content["source_memory_ids"]
    assert "浅色日系" in profile.summary


def test_memory_retrieval_tool_uses_manager_store_when_available() -> None:
    store = InMemoryStore()
    store.save(memory_item("m1", "product", "用户上次关注了一个黑色通勤包。"))
    manager = MemoryManager(store)

    result = MemoryRetrievalTool().run(
        {"user_id": "u1", "query": "黑色通勤包"},
        ToolContext(user_id="u1", session_id="s1", metadata={"memory_manager": manager}),
    )

    assert result.success is True
    assert result.data is not None
    assert result.data["items"][0]["memory_id"] == "m1"
    assert result.contract is not None
    assert result.contract.metadata["provider"] == "local"


def test_memory_retrieval_tool_ignores_noncanonical_action_field() -> None:
    store = InMemoryStore()
    store.save(memory_item("m1", "product", "用户上次关注了一个黑色通勤包。"))
    manager = MemoryManager(store)

    result = MemoryRetrievalTool().run(
        {"action": "get", "user_id": "u1", "query": "黑色通勤包"},
        ToolContext(user_id="u1", session_id="s1", metadata={"memory_manager": manager}),
    )

    assert result.success is True
    assert result.data is not None
    assert result.data["items"][0]["memory_id"] == "m1"


def test_memory_retrieval_tool_still_returns_missing_query_error() -> None:
    result = MemoryRetrievalTool().run({"action": "get", "user_id": "u1"})

    assert result.success is False
    assert result.error == "缺少检索 query，无法检索记忆"
