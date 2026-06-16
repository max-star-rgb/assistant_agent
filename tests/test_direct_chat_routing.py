from multimodal_agent.agent.runtime import AgentGraphRuntime
from multimodal_agent.memory.store import InMemoryStore
from multimodal_agent.schemas.requests import UserRequest


def test_direct_chat_uses_chat_adapter_without_tool_calls() -> None:
    state = AgentGraphRuntime(memory_store=InMemoryStore()).run_state(
        UserRequest(user_id="u1", session_id="s1", text="帮我写一段商品介绍")
    )

    assert state.status == "completed"
    assert state.intent is not None
    assert state.intent.intent == "direct_chat"
    assert state.tool_calls == []
    assert state.response is not None
    assert state.response.data["provider"] == "mock"
    assert state.response.data["model"] == "mock-direct-chat"
    assert "帮我写一段商品介绍" in state.response.message


def test_direct_chat_with_media_context_does_not_trigger_vision_understanding() -> None:
    state = AgentGraphRuntime(memory_store=InMemoryStore()).run_state(
        UserRequest(user_id="u1", session_id="s1", text="给我三个搭配建议", image_ids=["img1"])
    )

    assert state.status == "completed"
    assert state.intent is not None
    assert state.intent.intent == "direct_chat"
    assert [call.tool_name for call in state.tool_calls] == []
    assert state.response is not None
    assert state.response.data["provider"] == "mock"
