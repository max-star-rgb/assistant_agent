from assistant_agent.agent.prompt_builder import build_image_generation_request
from assistant_agent.agent.tool_input_builder import build_tool_input
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.tools.shopping_search_tool import ShoppingSearchTool


def test_shopping_search_output_flows_into_image_generation_prompt() -> None:
    search_result = _shopping_result()

    image_request = build_image_generation_request(
        UserRequest(user_id="u1", session_id="s1", text="生成一张商品海报"),
        {"step_1": search_result},
    )

    assert image_request.product_id == "p2"
    assert image_request.product_title == "简约白色板鞋 B"
    assert "简约白色板鞋 B" in image_request.prompt
    assert "价格更低，外观接近" in image_request.prompt


def test_shopping_search_output_flows_into_render_3d_input() -> None:
    search_result = _shopping_result()

    render_input = build_tool_input(
        "render_3d",
        UserRequest(user_id="u1", session_id="s1", text="放到北欧风客厅看看"),
        {"step_1": search_result},
    )

    assert render_input["product_id"] == "p1"
    assert render_input["image_url"] == "mock://images/p1.png"
    assert render_input["scene"] == "放到北欧风客厅看看"


def test_shopping_search_best_offer_can_feed_image_generation() -> None:
    search_result = _shopping_result()

    image_request = build_image_generation_request(
        UserRequest(user_id="u1", session_id="s1", text="生成一张商品海报"),
        {"step_2": search_result},
    )

    assert image_request.product_id == "p2"
    assert image_request.product_title == "简约白色板鞋 B"


def _shopping_result() -> ToolResult:
    result = ShoppingSearchTool().run({"query": "白色低帮运动鞋"})
    assert result.success is True
    return result
