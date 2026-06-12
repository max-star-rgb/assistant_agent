import pytest

from multimodal_agent.schemas.products import PriceCompareResult, ProductResult
from multimodal_agent.services.product_adapter import MockProductSearchAdapter, PriceCompareInput, ProductSearchInput
from multimodal_agent.tools.price_compare_tool import PriceCompareTool
from multimodal_agent.tools.product_search_tool import ProductSearchTool


def test_mock_product_adapter_search_returns_product_schema_list() -> None:
    products = MockProductSearchAdapter().search(ProductSearchInput(query="白色低帮运动鞋"))

    assert products
    assert all(isinstance(product, ProductResult) for product in products)
    assert all(product.product_id for product in products)


def test_mock_product_adapter_compare_returns_price_compare_schema() -> None:
    adapter = MockProductSearchAdapter()
    products = adapter.search(ProductSearchInput(query="白色低帮运动鞋"))

    result = adapter.compare(PriceCompareInput(items=products, query="白色低帮运动鞋"))

    assert isinstance(result, PriceCompareResult)
    assert result.items[0].price <= result.items[-1].price
    assert result.best_value_product_id == result.items[0].product_id


def test_mock_product_adapter_rejects_missing_search_query() -> None:
    with pytest.raises(ValueError, match="缺少商品描述"):
        MockProductSearchAdapter().search(ProductSearchInput())


def test_product_tools_return_structured_results_without_provider_details() -> None:
    search_result = ProductSearchTool(adapter=MockProductSearchAdapter()).run({"query": "白色低帮运动鞋"})
    compare_result = PriceCompareTool(adapter=MockProductSearchAdapter()).run(
        {"query": "白色低帮运动鞋", "items": search_result.data["items"]}
    )

    assert search_result.success is True
    assert compare_result.success is True
    assert search_result.data["items"]
    assert compare_result.data["best_value_product_id"]
