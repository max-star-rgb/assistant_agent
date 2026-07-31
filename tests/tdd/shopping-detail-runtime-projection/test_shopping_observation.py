from __future__ import annotations

from assistant_agent.tools.plugins.builtin.shopping.models import (
    PriceCompareResult,
    ProductResult,
    ProductSearchResult,
)
from assistant_agent.tools.plugins.builtin.shopping.tool import ShoppingSearchTool


class _SearchAdapter:
    def search(self, request: object) -> ProductSearchResult:
        query = str(getattr(request, "query"))
        return ProductSearchResult(
            items=[
                ProductResult(
                    product_id="p1",
                    provider_item_id="provider-p1",
                    title="小米14 12+256GB",
                    price=2599.0,
                    effective_price=2599.0,
                    platform="jd",
                    shop="京东",
                    product_url="https://u.jd.com/one",
                    image_url="https://img.example/one.jpg",
                    url_status="unverified",
                    availability="unknown",
                )
            ],
            provider="offline",
            query_used=query,
            total=1,
            output_ref="offline://search/xiaomi14",
        )


class _CompareAdapter:
    def compare(self, request: object) -> PriceCompareResult:
        return PriceCompareResult(
            query=str(getattr(request, "query")),
            items=list(getattr(request, "items")),
            summary="保留唯一候选。",
            provider="offline",
            output_ref="offline://compare/xiaomi14",
        )


def test_shopping_observation_exposes_one_minimal_item_list() -> None:
    """Catches duplicated shopping result layers or delivery-only URLs leaking into the prompt."""

    result = ShoppingSearchTool(
        search_adapter=_SearchAdapter(),
        compare_adapter=_CompareAdapter(),
    ).run({"needs": [{"keyword": "小米14"}]})

    assert result.success is True
    assert result.model_observation == {
        "outcome": "success",
        "total_cost": 2599.0,
        "within_budget": True,
        "summary": "已选出 1 项商品候选，合计 2599.00 元。",
        "items": [
            {
                "product_id": "p1",
                "need": "小米14",
                "title": "小米14 12+256GB",
                "platform": "jd",
                "shop": "京东",
                "quantity": 1,
                "total_price": 2599.0,
                "currency": "CNY",
                "url_status": "unverified",
                "availability": "unknown",
            }
        ],
    }
