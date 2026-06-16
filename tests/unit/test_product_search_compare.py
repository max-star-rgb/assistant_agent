from multimodal_agent.services.product_adapter import (
    MockProductSearchAdapter,
    PriceCompareInput,
    ProductSearchInput,
)
from multimodal_agent.tools.price_compare_tool import PriceCompareTool
from multimodal_agent.tools.product_search_tool import ProductSearchTool


def test_mock_product_search_returns_products_for_white_low_top_sneaker() -> None:
    adapter = MockProductSearchAdapter()

    result = adapter.search(ProductSearchInput(query="白色低帮运动鞋"))
    products = result.items

    assert result.provider == "mock"
    assert len(products) == 3
    assert products[0].title == "白色低帮运动鞋 A"
    assert all(product.platform for product in products)
    assert all(product.similarity is not None for product in products)
    assert all(product.reason for product in products)


def test_product_search_tool_returns_structured_tool_result() -> None:
    result = ProductSearchTool(adapter=MockProductSearchAdapter()).run(
        {"query": "白色低帮运动鞋"}
    )

    assert result.success is True
    assert result.data is not None
    assert result.data["provider"] == "mock"
    assert len(result.data["items"]) == 3
    assert result.data["items"][0]["title"] == "白色低帮运动鞋 A"
    assert result.data["items"][0]["reason"]


def test_product_search_tool_returns_structured_error_without_description() -> None:
    result = ProductSearchTool().run({})

    assert result.success is False
    assert result.error == "缺少商品描述，无法搜索"
    assert result.data["errors"][0]["code"] == "product_query_empty"


def test_mock_price_compare_sorts_by_ascending_price() -> None:
    adapter = MockProductSearchAdapter()
    products = adapter.search(ProductSearchInput(query="白色低帮运动鞋")).items

    result = adapter.compare(PriceCompareInput(items=products, query="白色低帮运动鞋"))

    prices = [item.price for item in result.items]
    assert prices == sorted(prices)
    assert [item.product_id for item in result.items] == ["p2", "p1", "p3"]
    assert result.best_value_product_id == "p2"
    assert result.offers[0].product_id == "p2"
    assert result.best_offer.product_id == "p2"


def test_price_compare_tool_sorts_by_ascending_price() -> None:
    search_result = ProductSearchTool().run({"query": "白色低帮运动鞋"})
    assert search_result.data is not None

    compare_result = PriceCompareTool().run(
        {"items": search_result.data["items"], "query": "白色低帮运动鞋"}
    )

    assert compare_result.success is True
    assert compare_result.data is not None
    prices = [item["price"] for item in compare_result.data["items"]]
    assert prices == sorted(prices)
    assert all(item["reason"] for item in compare_result.data["items"])
    assert compare_result.data["offers"][0]["product_id"] == "p2"
    assert compare_result.data["best_offer"]["product_id"] == "p2"


def test_price_compare_tool_returns_structured_error_without_items() -> None:
    result = PriceCompareTool().run({"items": []})

    assert result.success is False
    assert result.error == "缺少商品候选列表，无法比价"
    assert result.data["errors"][0]["code"] == "price_no_products"
