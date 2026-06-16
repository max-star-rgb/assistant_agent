from multimodal_agent.agent.runtime import AgentGraphRuntime
from multimodal_agent.memory.store import InMemoryStore
from multimodal_agent.schemas.requests import UserRequest


def test_text_only_image_generation_does_not_require_media() -> None:
    state = AgentGraphRuntime(memory_store=InMemoryStore()).run_state(
        UserRequest(user_id="u1", session_id="s1", text="生成一张赛博朋克风格海报")
    )

    assert state.status == "completed"
    assert state.intent is not None
    assert state.intent.intent == "image_generation"
    assert state.request.image_ids == []
    assert state.request.video_ids == []
    assert [call.tool_name for call in state.tool_calls] == ["image_generation"]
    assert state.tool_calls[0].input["reference_image_ids"] == []
    assert state.tool_results[0].output_ref == "local://generated/poster.png"


def test_text_only_image_generation_does_not_trigger_vision_understanding() -> None:
    state = AgentGraphRuntime(memory_store=InMemoryStore()).run_state(
        UserRequest(user_id="u1", session_id="s1", text="帮我生成一张日系极简商品图")
    )

    assert "vision_understanding" not in [call.tool_name for call in state.tool_calls]
    assert state.response is not None
    assert state.response.data["image_url"] == "local://generated/poster.png"
