from assistant_agent.tools.memory_tool import MemoryRetrievalTool, MemorySaveTool, MemoryTool


def test_memory_tool_retrieves_and_saves_stable_items() -> None:
    retrieve = MemoryTool().run(
        {"action": "retrieve", "user_id": "u1", "query": "上次那个黑色包"}
    )
    save = MemoryTool().run(
        {"action": "save", "user_id": "u1", "content": {"summary": "用户喜欢日系风格", "style": "日系"}}
    )

    assert retrieve.success is True
    assert retrieve.data is not None
    assert retrieve.data["items"][0]["memory_id"] == "m1"
    assert save.success is True
    assert save.data is not None
    assert save.data["memory_id"] == "m_saved_1"


def test_memory_tool_returns_structured_errors() -> None:
    retrieve = MemoryTool().run({"action": "retrieve", "user_id": "u1"})
    save = MemoryTool().run({"action": "save", "user_id": "u1"})

    assert retrieve.success is False
    assert retrieve.error == "缺少检索 query，无法检索记忆"
    assert save.success is False
    assert save.error == "缺少保存内容，无法写入记忆"


def test_dedicated_memory_retrieval_ignores_extra_action() -> None:
    result = MemoryRetrievalTool().run({"action": "get", "user_id": "u1", "query": "上次那个黑色包"})

    assert result.success is True
    assert result.data is not None
    assert result.data["items"][0]["memory_id"] == "m1"


def test_dedicated_memory_tools_keep_runtime_missing_input_errors() -> None:
    retrieve = MemoryRetrievalTool().run({"action": "get", "user_id": "u1"})
    save = MemorySaveTool().run({"action": "save", "user_id": "u1", "content": {"style": "日系"}})

    assert retrieve.success is False
    assert retrieve.error == "缺少检索 query，无法检索记忆"
    assert save.success is False
    assert save.error == "缺少保存内容，无法写入记忆"
