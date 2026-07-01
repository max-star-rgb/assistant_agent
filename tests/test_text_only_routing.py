import pytest

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.memory.store import InMemoryStore
from assistant_agent.schemas.requests import UserRequest


TEXT_ONLY_CASES = [
    ("帮我写一段商品介绍", "direct_chat", []),
    ("生成一张赛博朋克风格海报", "image_generation", ["image_generation"]),
    ("帮我找 500 元以内的白色运动鞋", "product_search", ["product_search"]),
    ("比较一下 iPhone 15 和 iPhone 16 的价格", "price_compare", ["price_compare"]),
    ("上次那个黑色包还在吗", "memory_retrieval", ["memory_retrieval"]),
    ("把浅灰色沙发放到北欧风客厅看看", "render_3d", ["render_3d"]),
]


@pytest.mark.parametrize(("text", "expected_intent", "expected_tools"), TEXT_ONLY_CASES)
def test_text_only_requests_route_to_assistant_capabilities(
    text: str,
    expected_intent: str,
    expected_tools: list[str],
) -> None:
    request = UserRequest(user_id="u1", session_id="s1", text=text)

    state = AgentGraphRuntime(memory_store=InMemoryStore()).run_state(request)

    assert state.intent is not None
    assert state.intent.intent == expected_intent
    assert request.image_ids == []
    assert request.video_ids == []
    assert [call.tool_name for call in state.tool_calls] == expected_tools
    assert "vision_understanding" not in [call.tool_name for call in state.tool_calls]
