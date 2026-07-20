from assistant_agent.services.product_adapter import (
    MockPriceCompareAdapter,
    MockProductSearchAdapter,
    ProductSearchInput,
)
from assistant_agent.tools.shopping_search_tool import ShoppingSearchTool
from assistant_agent.tools.registry import create_default_registry


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


def test_shopping_search_tool_returns_structured_tool_result() -> None:
    result = ShoppingSearchTool(
        search_adapter=MockProductSearchAdapter(),
        price_compare_adapter=MockPriceCompareAdapter(),
    ).run(
        {"query": "白色低帮运动鞋"}
    )

    assert result.success is True
    assert result.data is not None
    assert result.data["provider"] == "mock"
    assert len(result.data["search"]["items"]) == 3
    assert result.data["search"]["items"][0]["product_id"] == "p1"
    assert result.data["search"]["items"][0]["title"] == "白色低帮运动鞋 A"
    assert result.data["search"]["items"][0]["reason"]
    assert result.data["best_offer"]["product_id"] == "p2"


def test_shopping_search_tool_returns_structured_error_without_description() -> None:
    result = ShoppingSearchTool().run({})

    assert result.success is False
    assert result.error == "缺少商品描述，无法搜索"
    assert result.data["errors"][0]["code"] == "product_query_empty"


def test_shopping_search_tool_sorts_offers_by_ascending_price() -> None:
    result = ShoppingSearchTool().run({"query": "白色低帮运动鞋"})

    assert result.success is True
    assert result.data is not None
    prices = [item["price"] for item in result.data["items"]]
    assert prices == sorted(prices)
    assert all(item["reason"] for item in result.data["items"])
    assert result.data["best_value_product_id"] == "p2"
    assert result.data["offers"][0]["product_id"] == "p2"
    assert result.data["best_offer"]["product_id"] == "p2"


def test_shopping_search_tool_runs_search_then_price_compare() -> None:
    registry = create_default_registry()

    result = registry.run("shopping_search", {"query": "白色低帮运动鞋"})

    assert result.tool_name == "shopping_search"
    assert result.success is True
    assert result.data is not None
    assert result.data["query"] == "白色低帮运动鞋"
    assert result.data["search"]["total"] == 3
    assert len(result.data["search"]["items"]) == 3
    assert result.data["comparison"]["best_offer"]["product_id"] == "p2"
    assert result.data["best_offer"]["product_id"] == "p2"
    assert result.contract is not None
    assert result.contract.capability == "shopping_search"
    assert result.contract.data["best_offer"]["product_id"] == "p2"
