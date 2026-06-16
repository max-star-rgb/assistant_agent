from fastapi.testclient import TestClient

from multimodal_agent.api.app import create_app
from multimodal_agent.schemas.products import PriceOffer, ProductResult, RankingReason
from multimodal_agent.services.product_adapter import MockPriceCompareAdapter, MockProductSearchAdapter, PriceCompareInput, ProductSearchInput
from multimodal_agent.tools.product_search_tool import ProductSearchTool


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
