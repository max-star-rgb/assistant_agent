from langgraph.checkpoint.memory import MemorySaver

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.checkpointer import create_checkpointer


def test_langgraph_checkpointer_uses_memory_by_default() -> None:
    assert isinstance(create_checkpointer(ProviderConfig()), MemorySaver)


def test_langgraph_checkpointer_can_create_memory_saver() -> None:
    checkpointer = create_checkpointer(ProviderConfig(langgraph_checkpointer_backend="memory"))

    assert isinstance(checkpointer, MemorySaver)


def test_default_checkpointer_isolates_runs_with_same_session_id() -> None:
    runtime = AgentGraphRuntime()

    first = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="帮我找相似款"))
    second = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="生成一张日系海报"))

    assert first.run_id != second.run_id
    assert first.intent is not None
    assert first.intent.intent == "product_search"
    assert [call.tool_name for call in first.tool_calls] == ["shopping_search"]
    assert second.intent is not None
    assert second.intent.intent == "image_generation"
    assert [call.tool_name for call in second.tool_calls] == ["image_generation"]
    assert set(runtime.checkpointer.storage.keys()) == {first.run_id, second.run_id}
