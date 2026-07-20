from fastapi.testclient import TestClient

from assistant_agent.agent.response_templates import compose_contract_response
from assistant_agent.api.app import create_app
from assistant_agent.schemas.products import PriceOffer, ProductResult, RankingReason
from assistant_agent.schemas.tool_observation import observation_from_tool_result
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.product_adapter import MockPriceCompareAdapter, MockProductSearchAdapter, PriceCompareInput, ProductSearchInput
from assistant_agent.tools.shopping_search_tool import ShoppingSearchTool


def test_product_result_contract_exposes_stable_fields() -> None:
    result = MockProductSearchAdapter().search(ProductSearchInput(query="白色低帮运动鞋"))
    product = result.items[0]

    assert isinstance(product, ProductResult)
    assert product.product_id == "p1"
    assert product.title
    assert product.price >= 0
    assert product.currency == "CNY"
    assert product.platform
    assert product.product_url == "mock://shop-a/p1"
    assert product.image_url == "mock://images/p1.png"
    assert product.similarity_score == product.similarity
    assert product.reason
    assert isinstance(product.ranking_reason, RankingReason)
    assert product.ranking_reason.explanation
    assert product.source == "mock"


def test_product_result_serializes_link_status_without_provider_raw_response() -> None:
    product = ProductResult(
        product_id="AAE9r8X",
        provider_item_id="AAE9r8X",
        title="闲鱼加密 ID 商品",
        price=79.0,
        platform="haodanku",
        product_url=None,
        url_status="invalid_id",
        availability="unknown",
        source="haodanku",
    )

    payload = product.model_dump(mode="json")

    assert payload["provider_item_id"] == "AAE9r8X"
    assert payload["product_url"] is None
    assert payload["raw_url"] is None
    assert payload["url_status"] == "invalid_id"
    assert payload["availability"] == "unknown"
    assert "raw" not in payload
    assert "provider_response" not in payload


def test_price_offer_contract_exposes_stable_fields_and_ranking_reason() -> None:
    products = MockProductSearchAdapter().search(ProductSearchInput(query="白色低帮运动鞋")).items
    result = MockPriceCompareAdapter().compare(PriceCompareInput(items=products, query="白色低帮运动鞋"))

    assert result.success is True
    assert result.best_offer is not None
    assert isinstance(result.best_offer, PriceOffer)
    assert result.best_offer.offer_id
    assert result.best_offer.product_id == "p2"
    assert result.best_offer.total_price == result.best_offer.price
    assert result.best_offer.product_url == "mock://shop-b/p2"
    assert result.best_offer.reason
    assert result.ranking_reason is not None
    assert result.ranking_reason.explanation


def test_shopping_search_tool_output_does_not_expose_provider_raw_response() -> None:
    result = ShoppingSearchTool(
        search_adapter=MockProductSearchAdapter(),
        price_compare_adapter=MockPriceCompareAdapter(),
    ).run({"query": "白色低帮运动鞋"})

    assert result.success is True
    assert result.data["provider"] == "mock"
    assert "raw" not in result.data
    assert "provider_response" not in result.data
    assert "search" in result.data
    assert result.data["search"]["items"][0]["product_url"] == "mock://shop-a/p1"
    assert result.data["best_offer"]["product_url"] == "mock://shop-b/p2"


def test_shopping_search_observation_includes_user_visible_product_url() -> None:
    result = ShoppingSearchTool(
        search_adapter=MockProductSearchAdapter(),
        price_compare_adapter=MockPriceCompareAdapter(),
    ).run({"query": "白色低帮运动鞋"})

    observation = observation_from_tool_result(result)

    assert observation.status == "succeeded"
    assert "简约白色板鞋 B" in observation.summary
    assert observation.structured_output["best_offer"]["product_url"] == "mock://shop-b/p2"


def test_shopping_search_observation_guides_final_purchase_advice() -> None:
    result = ShoppingSearchTool(
        search_adapter=MockProductSearchAdapter(),
        price_compare_adapter=MockPriceCompareAdapter(),
    ).run({"query": "白色低帮运动鞋"})

    observation = observation_from_tool_result(
        result,
        request_text="帮我找一款白色低帮运动鞋。",
    )

    assert observation.status == "succeeded"
    assert observation.next_step_hint is not None
    assert "structured_output.best_offer" in observation.next_step_hint
    assert "不要声称已经下单" in observation.next_step_hint


def test_shopping_search_observation_does_not_expose_old_compare_tool_hint() -> None:
    result = ShoppingSearchTool(
        search_adapter=MockProductSearchAdapter(),
        price_compare_adapter=MockPriceCompareAdapter(),
    ).run({"query": "蓝牙耳机"})

    observation = observation_from_tool_result(
        result,
        request_text="帮我买个蓝牙耳机，推荐个划算的。",
    )

    assert observation.status == "succeeded"
    assert observation.next_step_hint is not None
    assert "price_compare" not in observation.next_step_hint


def test_repeated_shopping_search_failure_observation_points_to_prior_success() -> None:
    failed_result = ToolResult(
        tool_name="shopping_search",
        success=False,
        error="unknown_error: 数据已获取完毕或获取数据失败!",
    )

    observation = observation_from_tool_result(
        failed_result,
        request_text="帮我找一款商品",
        prior_observations=[
            {
                "tool_name": "shopping_search",
                "status": "succeeded",
                "summary": "Top product: 商品 A.",
            }
        ],
    )

    assert observation.status == "failed"
    assert observation.next_step_hint is not None
    assert "previous shopping_search call already succeeded" in observation.next_step_hint
    assert "partial results" in observation.next_step_hint


def test_shopping_search_observation_does_not_show_invalid_synthetic_product_url() -> None:
    product = ProductResult(
        product_id="AAE9r8X",
        provider_item_id="AAE9r8X",
        title="闲鱼加密 ID 商品",
        price=79.0,
        platform="haodanku",
        product_url=None,
        url_status="invalid_id",
        availability="unknown",
        source="haodanku",
    )
    tool_result = ToolResult(
        tool_name="shopping_search",
        success=True,
        data={
            "best_offer": product.model_dump(mode="json"),
            "offers": [product.model_dump(mode="json")],
            "summary": "找到闲鱼加密 ID 商品。",
        },
    )

    observation = observation_from_tool_result(tool_result)
    message = compose_contract_response(
        [
            {
                "status": "succeeded",
                "capability": "shopping_search",
                "data": {"best_offer": product.model_dump(mode="json")},
            }
        ],
        [],
    )

    assert observation.status == "succeeded"
    assert "no direct product url" in observation.summary
    assert "item.taobao.com" not in observation.summary
    assert "未提供可直接打开的商品链接" in message
    assert "item.taobao.com" not in message
