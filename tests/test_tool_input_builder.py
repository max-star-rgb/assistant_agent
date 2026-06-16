from multimodal_agent.agent.tool_input_builder import build_tool_input
from multimodal_agent.schemas.requests import UserRequest
from multimodal_agent.schemas.tools import ToolResult


def test_build_tool_input_uses_visual_summary_for_product_search() -> None:
    request = UserRequest(user_id="u1", session_id="s1", text="找同款")
    outputs = {
        "s1": ToolResult(
            tool_name="vision_understanding",
            success=True,
            data={"summary": "白色低帮运动鞋"},
        )
    }

    tool_input = build_tool_input("search_product", request, outputs)

    assert tool_input == {"query": "找同款", "visual_summary": "白色低帮运动鞋"}


def test_build_tool_input_keeps_prompt_for_image_generation_without_products() -> None:
    request = UserRequest(user_id="u1", session_id="s1", text="生成一张日系海报")

    tool_input = build_tool_input("generate_image", request, {})

    assert "生成一张日系海报" in tool_input["prompt"]
    assert tool_input["style"] == "日系海报"
