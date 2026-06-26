from datetime import datetime, timezone

from multimodal_agent.agent.state import AgentState
from multimodal_agent.memory.manager import MemoryManager
from multimodal_agent.memory.profile import USER_PROFILE_MEMORY_ID
from multimodal_agent.memory.store import InMemoryStore
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
    assert "语义记忆" in state.request.metadata["memory_context_text"]
    assert "情景记忆" in state.request.metadata["memory_context_text"]
    assert state.request.metadata["memory_context_summaries"] == [
        "用户喜欢日系极简风格。",
        "曾经先搜索商品再比较价格。",
    ]


def test_memory_manager_saves_completed_run_summary() -> None:
    store = InMemoryStore()
    manager = MemoryManager(store)
    request = UserRequest(user_id="u1", session_id="s1", text="帮我找白鞋")
    state = AgentState.from_request(request)
    state.set_response(AgentResponse(message="已完成商品搜索。"))

    item = manager.save_from_run(state)

    assert item is not None
    assert item.memory_type == "task"
    assert store.search(MemoryQuery(user_id="u1", query="商品搜索")).items


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
            "action": "save",
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
        {"action": "retrieve", "user_id": "u1", "query": "黑色通勤包"},
        ToolContext(user_id="u1", session_id="s1", metadata={"memory_manager": manager}),
    )

    assert result.success is True
    assert result.data is not None
    assert result.data["items"][0]["memory_id"] == "m1"
    assert result.contract is not None
    assert result.contract.metadata["provider"] == "local"
