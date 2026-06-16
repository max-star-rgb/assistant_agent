import pytest

from multimodal_agent.agent.runtime import AgentGraphRuntime
from multimodal_agent.memory.store import InMemoryStore
from multimodal_agent.schemas.requests import UserRequest


def run_request(request: UserRequest):
    return AgentGraphRuntime(memory_store=InMemoryStore()).run_state(request)


@pytest.mark.parametrize(
    ("user_request", "expected_intent", "expected_tools"),
    [
        (
            UserRequest(user_id="u1", session_id="s1", text="看看图里有什么", image_ids=["img1"]),
            "image_understanding",
            ["vision_understanding"],
        ),
        (
            UserRequest(user_id="u1", session_id="s1", text="总结这个视频", video_ids=["video1"]),
            "video_understanding",
            ["video_understanding"],
        ),
        (
            UserRequest(user_id="u1", session_id="s1", text="用这张图生成海报", image_ids=["img1"]),
            "multi_step_orchestration",
            ["vision_understanding", "image_generation"],
        ),
        (
            UserRequest(user_id="u1", session_id="s1", text="找同款并比价", image_ids=["img1"]),
            "multi_step_orchestration",
            ["vision_understanding", "product_search", "price_compare"],
        ),
        (
            UserRequest(user_id="u1", session_id="s1", text="找视频里的商品", video_ids=["video1"]),
            "multi_step_orchestration",
            ["video_understanding", "product_search"],
        ),
        (
            UserRequest(user_id="u1", session_id="s1", text="帮我写一段商品介绍", image_ids=["img1"]),
            "direct_chat",
            [],
        ),
    ],
)
def test_media_aware_requests_route_by_text_intent(
    user_request: UserRequest,
    expected_intent: str,
    expected_tools: list[str],
) -> None:
    state = run_request(user_request)

    assert state.intent is not None
    assert state.intent.intent == expected_intent
    assert [call.tool_name for call in state.tool_calls] == expected_tools


@pytest.mark.parametrize(
    ("user_request", "missing_slot"),
    [
        (UserRequest(user_id="u1", session_id="s1", text="看看图里有什么"), "image"),
        (UserRequest(user_id="u1", session_id="s1", text="总结这个视频"), "video"),
        (UserRequest(user_id="u1", session_id="s1", text="这个", image_ids=["img1"]), "context"),
    ],
)
def test_media_aware_routing_asks_followup_for_missing_or_unclear_context(
    user_request: UserRequest,
    missing_slot: str,
) -> None:
    state = run_request(user_request)

    assert state.intent is not None
    assert state.intent.intent == "ask_followup"
    assert missing_slot in state.intent.missing_slots
    assert state.tool_calls == []
