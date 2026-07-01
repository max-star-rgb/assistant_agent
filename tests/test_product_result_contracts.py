from fastapi.testclient import TestClient

from assistant_agent.agent.response_templates import compose_contract_response
from assistant_agent.api.app import create_app
from assistant_agent.schemas.products import PriceOffer, ProductResult, ProductSearchResult, RankingReason
from assistant_agent.schemas.tool_observation import observation_from_tool_result
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.product_adapter import MockPriceCompareAdapter, MockProductSearchAdapter, PriceCompareInput, ProductSearchInput
from assistant_agent.tools.product_search_tool import ProductSearchTool


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


def test_product_search_tool_output_does_not_expose_provider_raw_response() -> None:
    result = ProductSearchTool(adapter=MockProductSearchAdapter()).run({"query": "白色低帮运动鞋"})

    assert result.success is True
    assert result.data["provider"] == "mock"
    assert "raw" not in result.data
    assert "provider_response" not in result.data
    assert "items" in result.data
    assert result.data["items"][0]["product_url"] == "mock://shop-a/p1"


def test_product_search_observation_includes_user_visible_product_url() -> None:
    result = ProductSearchTool(adapter=MockProductSearchAdapter()).run({"query": "白色低帮运动鞋"})

    observation = observation_from_tool_result(result)

    assert observation.status == "succeeded"
    assert "白色低帮运动鞋 A" in observation.summary
    assert "mock://shop-a/p1" in observation.summary


def test_product_search_observation_hints_price_compare_when_requested() -> None:
    result = ProductSearchTool(adapter=MockProductSearchAdapter()).run({"query": "白色低帮运动鞋"})

    observation = observation_from_tool_result(
        result,
        request_text="帮我找一款白色低帮运动鞋，并比较一下价格。",
    )

    assert observation.status == "succeeded"
    assert observation.next_step_hint is not None
    assert "Call price_compare next" in observation.next_step_hint
    assert "structured_output.items as full product objects" in observation.next_step_hint
    assert "not title strings" in observation.next_step_hint
    assert "do not run product_search again" in observation.next_step_hint


def test_repeated_product_search_failure_observation_points_to_prior_success() -> None:
    failed_result = ToolResult(
        tool_name="product_search",
        success=False,
        error="unknown_error: 数据已获取完毕或获取数据失败!",
    )

    observation = observation_from_tool_result(
        failed_result,
        request_text="帮我找一款商品",
        prior_observations=[
            {
                "tool_name": "product_search",
                "status": "succeeded",
                "summary": "Top product: 商品 A.",
            }
        ],
    )

    assert observation.status == "failed"
    assert observation.next_step_hint is not None
    assert "previous product_search call already succeeded" in observation.next_step_hint
    assert "partial results" in observation.next_step_hint


def test_product_search_observation_does_not_show_invalid_synthetic_product_url() -> None:
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
    search_result = ProductSearchResult(
        items=[product],
        provider="haodanku",
        query_used="闲鱼",
        total=1,
    )
    tool_result = ToolResult(
        tool_name="product_search",
        success=True,
        data=search_result.model_dump(mode="json"),
    )

    observation = observation_from_tool_result(tool_result)
    message = compose_contract_response(
        [
            {
                "status": "succeeded",
                "capability": "product_search",
                "data": {"items": [product.model_dump(mode="json")], "total": 1},
            }
        ],
        [],
    )

    assert observation.status == "succeeded"
    assert "no direct product url" in observation.summary
    assert "item.taobao.com" not in observation.summary
    assert "未提供可直接打开的商品链接" in message
    assert "item.taobao.com" not in message


def test_product_search_api_output_contract_is_stable() -> None:
    client = TestClient(create_app())

    response = client.post(
        "/agent/run",
        json={"user_id": "u1", "session_id": "s1", "text": "帮我找相似款"},
    )

    assert response.status_code == 200
    payload = response.json()
    tool_result = payload["tool_results"][0]
    product = tool_result["data"]["items"][0]
    assert tool_result["success"] is True
    assert product["product_id"]
    assert product["product_url"] == "mock://shop-a/p1"
    assert product["source"] == "mock"
    assert product["ranking_reason"]["explanation"]
    assert "provider_response" not in tool_result["data"]
