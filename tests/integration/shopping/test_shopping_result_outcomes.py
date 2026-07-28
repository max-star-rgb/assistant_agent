"""Regression coverage for assistant-facing shopping result outcomes."""

from assistant_agent.tools.plugins.builtin.shopping.models import (
    PriceCompareResult,
    ProductProviderError,
    ProductResult,
    ProductSearchResult,
)
from assistant_agent.tools.plugins.builtin.shopping.tool import ShoppingSearchTool


class _SearchAdapter:
    def __init__(self, result: ProductSearchResult) -> None:
        self.result = result

    def search(self, _request):
        return self.result


class _CompareAdapter:
    def __init__(self, result: PriceCompareResult) -> None:
        self.result = result

    def compare(self, _request):
        return self.result


def _product() -> ProductResult:
    return ProductResult(
        product_id="bag-1",
        title="通勤电脑双肩包",
        price=299,
        platform="taobao",
        product_url="https://example.com/bag-1",
    )


def test_shopping_provider_failure_is_explicit_and_omits_internal_output_ref() -> None:
    failure = ProductProviderError(
        code="provider_bad_response",
        message="好单库未返回可用商品数据。",
        recoverable=False,
    )
    tool = ShoppingSearchTool(
        search_adapter=_SearchAdapter(
            ProductSearchResult(
                provider="haodanku",
                query_used="通勤电脑双肩包",
                errors=[failure],
                failed_platforms=["taobao"],
                output_ref="haodanku://search/internal",
            )
        ),
        compare_adapter=_CompareAdapter(
            PriceCompareResult(
                query="unused",
                summary="unused",
                provider="unused",
            )
        ),
    )

    result = tool.run(
        {"query": "通勤电脑双肩包", "budget_max": 500}
    )

    assert result.success is False
    assert result.data is not None and result.data["outcome"] == "failed"
    assert result.model_observation is not None
    assert result.model_observation["outcome"] == "failed"
    assert result.model_observation["requested_constraints"] == {
        "budget_max": 500.0
    }
    assert result.model_observation["errors"] == [
        failure.model_dump(mode="json")
    ]
    assert "output_ref" not in result.model_observation


def test_empty_shopping_search_is_a_completed_empty_result() -> None:
    tool = ShoppingSearchTool(
        search_adapter=_SearchAdapter(
            ProductSearchResult(
                provider="haodanku",
                query_used="不存在的商品",
            )
        ),
        compare_adapter=_CompareAdapter(
            PriceCompareResult(
                query="unused",
                summary="unused",
                provider="unused",
            )
        ),
    )

    result = tool.run({"query": "不存在的商品"})

    assert result.success is True
    assert result.data is not None and result.data["outcome"] == "empty"
    assert result.data["errors"] == []
    assert result.model_observation is not None
    assert result.model_observation["outcome"] == "empty"
    assert result.model_observation["summary"] == "未找到符合条件的商品。"


def test_usable_candidates_survive_comparison_failure_as_partial_result() -> None:
    comparison_error = ProductProviderError(
        code="provider_unavailable",
        message="比价服务暂不可用。",
        recoverable=True,
    )
    tool = ShoppingSearchTool(
        search_adapter=_SearchAdapter(
            ProductSearchResult(
                items=[_product()],
                provider="haodanku",
                query_used="通勤电脑双肩包",
                total=1,
                succeeded_platforms=["taobao"],
            )
        ),
        compare_adapter=_CompareAdapter(
            PriceCompareResult(
                query="通勤电脑双肩包",
                items=[_product()],
                summary="比价服务暂不可用。",
                provider="haodanku",
                errors=[comparison_error],
            )
        ),
    )

    result = tool.run({"query": "通勤电脑双肩包", "budget_max": 500})

    assert result.success is True
    assert result.data is not None and result.data["outcome"] == "partial"
    assert result.model_observation is not None
    assert result.model_observation["outcome"] == "partial"
    assert result.model_observation["items"][0]["product_id"] == "bag-1"
    assert result.model_observation["errors"] == [
        comparison_error.model_dump(mode="json")
    ]
