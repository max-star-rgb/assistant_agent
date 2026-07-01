from assistant_agent.agent.prompt_builder import build_image_generation_request
from assistant_agent.agent.tool_input_builder import build_tool_input
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.product_adapter import MockPriceCompareAdapter, MockProductSearchAdapter, PriceCompareInput, ProductSearchInput


def test_product_search_output_flows_into_price_compare() -> None:
    products = MockProductSearchAdapter().search(ProductSearchInput(query="白色低帮运动鞋")).items
    search_result = ToolResult(
        tool_name="product_search",
        success=True,
        data={"items": [product.model_dump(mode="json") for product in products]},
    )

    tool_input = build_tool_input(
        "compare_price",
        UserRequest(user_id="u1", session_id="s1", text="比较价格"),
        {"step_1": search_result},
    )
    compare_result = MockPriceCompareAdapter().compare(PriceCompareInput.model_validate(tool_input))

    assert tool_input["items"][0]["product_id"] == "p1"
    assert compare_result.best_offer is not None
    assert compare_result.best_offer.product_id == "p2"


def test_product_search_output_flows_into_image_generation_prompt() -> None:
    products = MockProductSearchAdapter().search(ProductSearchInput(query="白色低帮运动鞋")).items
    search_result = ToolResult(
        tool_name="product_search",
        success=True,
        data={"items": [product.model_dump(mode="json") for product in products]},
    )

    image_request = build_image_generation_request(
        UserRequest(user_id="u1", session_id="s1", text="生成一张商品海报"),
        {"step_1": search_result},
    )

    assert image_request.product_id == "p1"
    assert image_request.product_title == "白色低帮运动鞋 A"
    assert "白色低帮运动鞋 A" in image_request.prompt
    assert "颜色、鞋型和材质相似度最高" in image_request.prompt


def test_product_search_output_flows_into_render_3d_input() -> None:
    products = MockProductSearchAdapter().search(ProductSearchInput(query="白色低帮运动鞋")).items
    search_result = ToolResult(
        tool_name="product_search",
        success=True,
        data={"items": [product.model_dump(mode="json") for product in products]},
        output_ref="mock://products/white-low-top-sneaker",
    )

    render_input = build_tool_input(
        "render_3d",
        UserRequest(user_id="u1", session_id="s1", text="放到北欧风客厅看看"),
        {"step_1": search_result},
    )

    assert render_input["product_id"] == "p1"
    assert render_input["image_url"] == "mock://images/p1.png"
    assert render_input["scene"] == "放到北欧风客厅看看"


def test_price_compare_output_can_feed_image_generation_with_best_offer_order() -> None:
    products = MockProductSearchAdapter().search(ProductSearchInput(query="白色低帮运动鞋")).items
    compare_result = MockPriceCompareAdapter().compare(
        PriceCompareInput(items=products, query="白色低帮运动鞋")
    )
    compare_tool_result = ToolResult(
        tool_name="price_compare",
        success=True,
        data=compare_result.model_dump(mode="json"),
    )

    image_request = build_image_generation_request(
        UserRequest(user_id="u1", session_id="s1", text="生成一张商品海报"),
        {"step_2": compare_tool_result},
    )

    assert image_request.product_id == "p2"
    assert image_request.product_title == "简约白色板鞋 B"
