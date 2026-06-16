from multimodal_agent.agent.runtime import AgentGraphRuntime
from multimodal_agent.memory.store import InMemoryStore
from multimodal_agent.schemas.requests import UserRequest


def _run(text: str, video_ids: list[str] | None = None):
    return AgentGraphRuntime(memory_store=InMemoryStore()).run_state(
        UserRequest(user_id="u1", session_id="s1", text=text, video_ids=["video1"] if video_ids is None else video_ids)
    )


def test_single_step_video_summary_uses_video_understanding_tool() -> None:
    state = _run("总结这个视频")

    assert state.status == "completed"
    assert [call.tool_name for call in state.tool_calls] == ["video_understanding"]
    assert state.tool_results[0].data["summary"]
    assert "我先理解了视频内容" in state.response.message


def test_video_to_product_search_flow() -> None:
    state = _run("找视频里的商品")

    assert [call.tool_name for call in state.tool_calls] == ["video_understanding", "product_search"]
    assert state.tool_calls[1].input["video_summary"]
    assert state.tool_results[1].success is True


def test_video_to_product_search_to_price_compare_flow() -> None:
    state = _run("找视频里的商品，并比较价格")

    assert [call.tool_name for call in state.tool_calls] == [
        "video_understanding",
        "product_search",
        "price_compare",
    ]
    assert state.tool_calls[2].input["items"]
    assert state.tool_results[2].success is True


def test_video_to_image_generation_flow() -> None:
    state = _run("根据这个视频里的商品生成一张宣传海报")

    assert [call.tool_name for call in state.tool_calls] == ["video_understanding", "image_generation"]
    assert "视频中展示了一双白色低帮运动鞋" in state.tool_calls[1].input["prompt"]


def test_video_to_render_flow() -> None:
    state = _run("把视频里的商品做一个展厅 3D 展示")

    assert [call.tool_name for call in state.tool_calls] == ["video_understanding", "render_3d"]
    assert state.tool_calls[1].input["video_ref"] == "mock://video/understanding/video1"
    assert state.tool_results[1].success is True


def test_video_to_memory_save_flow() -> None:
    state = _run("记住这个视频里的商品风格")

    assert [call.tool_name for call in state.tool_calls] == ["video_understanding", "memory_save"]
    assert state.tool_calls[1].input["content"]["summary"]
    assert state.tool_results[1].success is True


def test_video_request_missing_video_asks_followup() -> None:
    state = _run("总结这个视频", video_ids=[])

    assert state.intent is not None
    assert state.intent.intent == "ask_followup"
    assert state.tool_calls == []


def test_video_present_but_direct_chat_does_not_force_video_understanding() -> None:
    state = _run("帮我写一段商品介绍", video_ids=["video1"])

    assert state.intent is not None
    assert state.intent.intent == "direct_chat"
    assert state.tool_calls == []
