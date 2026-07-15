import pytest

from assistant_agent.schemas.products import PriceCompareResult, ProductResult
from assistant_agent.services.product_adapter import MockProductSearchAdapter, PriceCompareInput, ProductSearchInput
from assistant_agent.tools.price_compare_tool import PriceCompareTool
from assistant_agent.tools.product_search_tool import ProductSearchTool


def test_mock_product_adapter_search_returns_product_schema_list() -> None:
    result = MockProductSearchAdapter().search(ProductSearchInput(query="白色低帮运动鞋"))

    assert result.success is True
    assert result.items
    assert result.provider == "mock"
    assert all(isinstance(product, ProductResult) for product in result.items)
    assert all(product.product_id for product in result.items)


def test_mock_product_adapter_compare_returns_price_compare_schema() -> None:
    adapter = MockProductSearchAdapter()
    products = adapter.search(ProductSearchInput(query="白色低帮运动鞋")).items

    result = adapter.compare(PriceCompareInput(items=products, query="白色低帮运动鞋"))

    assert isinstance(result, PriceCompareResult)
    prices = [item.price for item in result.items]
    assert prices == sorted(prices)
    assert [item.product_id for item in result.items] == ["p2", "p1", "p3"]
    assert result.best_value_product_id == "p2"
    assert result.offers[0].product_id == "p2"
    assert result.best_offer.product_id == "p2"


def test_mock_product_adapter_rejects_missing_search_query() -> None:
    result = MockProductSearchAdapter().search(ProductSearchInput())

    assert result.success is False
    assert result.errors[0].code == "product_query_empty"
    assert "缺少商品描述" in result.errors[0].message


def test_product_tools_return_structured_results_without_provider_details() -> None:
    search_result = ProductSearchTool(adapter=MockProductSearchAdapter()).run({"query": "白色低帮运动鞋"})
    compare_result = PriceCompareTool(adapter=MockProductSearchAdapter()).run(
        {"query": "白色低帮运动鞋", "items": search_result.data["items"]}
    )

    assert search_result.success is True
    assert compare_result.success is True
    assert search_result.data["items"]
    assert compare_result.data["best_value_product_id"]
