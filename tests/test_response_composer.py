from multimodal_agent.agent.response_composer import compose_response
from multimodal_agent.agent.state import AgentState
from multimodal_agent.schemas.planning import IntentResult
from multimodal_agent.schemas.requests import UserRequest
from multimodal_agent.schemas.tools import ToolResult


def test_compose_response_includes_price_and_image_result() -> None:
    state = AgentState.from_request(UserRequest(user_id="u1", session_id="s1", text="test"))
    state.set_intent(IntentResult(intent="multi_tool_task", confidence=0.9, rationale="test"))
    state.tool_results.append(
        ToolResult(
            tool_name="price_compare",
            success=True,
            data={"items": [{"title": "白色低帮运动鞋 A", "price": 259.0}]},
        )
    )
    state.tool_results.append(
        ToolResult(
            tool_name="image_generation",
            success=True,
            data={"image_url": "local://generated/poster.png"},
        )
    )

    response = compose_response(state)

    assert "最低价格" in response.message
    assert "图片生成结果" in response.message
    assert response.data["product_title"] == "白色低帮运动鞋 A"
    assert response.data["image_url"] == "local://generated/poster.png"
