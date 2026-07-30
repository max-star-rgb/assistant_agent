"""Offline contracts for the unified single- and multi-need shopping search."""

import pytest
from pydantic import ValidationError

from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.plugins.builtin.shopping.models import (
    PriceCompareResult,
    ProductProviderError,
    ProductResult,
    ProductSearchResult,
    ShoppingSearchRequest,
)
from assistant_agent.tools.plugins.builtin.shopping.tool import ShoppingSearchTool


class _RecordingSearchAdapter:
    provider = "recording"

    def __init__(self, results: dict[str, ProductSearchResult]) -> None:
        self.results = results
        self.queries: list[str] = []

    def search(self, request: object) -> ProductSearchResult:
        query = str(getattr(request, "query"))
        self.queries.append(query)
        return self.results[query]


class _PassthroughCompareAdapter:
    def compare(self, request) -> PriceCompareResult:
        return PriceCompareResult(
            query=request.query,
            items=list(request.items),
            summary="已保留搜索候选。",
            provider="recording",
        )


def _product(product_id: str, title: str, price: float) -> ProductResult:
    return ProductResult(
        product_id=product_id,
        title=title,
        price=price,
        effective_price=price,
        platform="测试平台",
    )


def _result(query: str, *items: ProductResult) -> ProductSearchResult:
    return ProductSearchResult(
        items=list(items),
        provider="recording",
        query_used=query,
        total=len(items),
        output_ref=f"recording://{query}",
        latency_ms=1,
    )


def test_shopping_search_queries_each_category_and_enforces_total_budget() -> None:
    adapter = _RecordingSearchAdapter(
        {
            "桌面电火锅": _result(
                "桌面电火锅",
                _product("pot-premium", "高配电火锅", 220),
                _product("pot-value", "基础电火锅", 120),
            ),
            "餐具": _result(
                "餐具",
                _product("cutlery", "餐具套装", 45),
            ),
            "火锅底料": _result(
                "火锅底料",
                _product("base", "火锅底料", 25),
            ),
        }
    )
    tool = ShoppingSearchTool(
        search_adapter=adapter,
        compare_adapter=_PassthroughCompareAdapter(),
    )

    result = tool.run(
        {
            "scenario": "两人室内聚餐",
            "decision_reason": "室外天气不适合，改为室内火锅",
            "evidence": [
                {
                    "source_tool": "weather",
                    "output_ref": "weather://shanghai/tomorrow",
                    "summary": "明天有雨。",
                }
            ],
            "total_budget": 240,
            "needs": [
                {"keyword": "桌面电火锅"},
                {"keyword": "餐具", "quantity": 2},
                {"keyword": "火锅底料"},
            ],
        },
        ToolContext(),
    )

    assert result.success is True
    assert adapter.queries == ["桌面电火锅", "餐具", "火锅底料"]
    assert result.data is not None
    assert result.data["outcome"] == "success"
    assert result.data["total_cost"] == 235
    assert result.data["within_budget"] is True
    assert [
        selection["product"]["product_id"]
        for selection in result.data["selections"]
    ] == ["pot-value", "cutlery", "base"]
    assert result.data["evidence"][0]["source_tool"] == "weather"


def test_shopping_search_reports_partial_when_budget_cannot_cover_required_need() -> None:
    adapter = _RecordingSearchAdapter(
        {
            "电火锅": _result("电火锅", _product("pot", "电火锅", 180)),
            "餐具": _result("餐具", _product("cutlery", "餐具", 80)),
        }
    )

    result = ShoppingSearchTool(
        search_adapter=adapter,
        compare_adapter=_PassthroughCompareAdapter(),
    ).run(
        {
            "scenario": "室内聚餐",
            "decision_reason": "准备两类用品",
            "total_budget": 200,
            "needs": [{"keyword": "电火锅"}, {"keyword": "餐具"}],
        }
    )

    assert result.success is True
    assert result.data is not None
    assert result.data["outcome"] == "partial"
    assert result.data["total_cost"] <= 200
    assert len(result.data["uncovered_required_needs"]) == 1
    assert sorted(need["status"] for need in result.data["needs"]) == [
        "budget_excluded",
        "selected",
    ]


def test_shopping_search_fails_only_when_all_item_searches_fail() -> None:
    error = ProductProviderError(
        code="provider_bad_response",
        message="provider failed",
        recoverable=False,
    )
    adapter = _RecordingSearchAdapter(
        {
            "电火锅": ProductSearchResult(
                provider="recording",
                query_used="电火锅",
                errors=[error],
            ),
            "餐具": ProductSearchResult(
                provider="recording",
                query_used="餐具",
                errors=[error],
            ),
        }
    )

    result = ShoppingSearchTool(
        search_adapter=adapter,
        compare_adapter=_PassthroughCompareAdapter(),
    ).run(
        {
            "scenario": "室内聚餐",
            "decision_reason": "准备用品",
            "total_budget": 300,
            "needs": [{"keyword": "电火锅"}, {"keyword": "餐具"}],
        }
    )

    assert result.success is False
    assert result.data is not None
    assert result.data["outcome"] == "failed"
    assert result.contract is not None
    assert all(error.recoverable is False for error in result.contract.errors)


def test_single_need_search_does_not_require_total_budget() -> None:
    adapter = _RecordingSearchAdapter(
        {"通勤电脑包": _result("通勤电脑包", _product("bag", "通勤电脑包", 299))}
    )

    result = ShoppingSearchTool(
        search_adapter=adapter,
        compare_adapter=_PassthroughCompareAdapter(),
    ).run({"needs": [{"keyword": "通勤电脑包"}]})

    assert result.success is True
    assert result.data is not None
    assert result.data["total_budget"] is None
    assert result.data["total_cost"] == 299
    assert result.data["within_budget"] is True


def test_multiple_needs_require_total_budget() -> None:
    with pytest.raises(ValidationError, match="total_budget"):
        ShoppingSearchRequest.model_validate(
            {
                "needs": [
                    {"keyword": "电火锅"},
                    {"keyword": "餐具"},
                ]
            }
        )
