"""Regression coverage for shopping relevance and price comparability."""

from assistant_agent.tools.plugins.builtin.shopping.models import PriceCompareRequest, ProductResult, ShoppingSearchRequest
from assistant_agent.tools.plugins.builtin.shopping.product_matching import compare_products, filter_products


def _product(product_id: str, title: str, price: float, *, platform: str = "taobao", score: float | None = None) -> ProductResult:
    return ProductResult(
        product_id=product_id,
        title=title,
        price=price,
        platform=platform,
        text_match_score=score,
    )


def test_unrelated_cheapest_search_candidate_is_not_declared_best_offer() -> None:
    items = [
        _product("bath-gel", "牛奶紫罗兰香氛沐浴露", 9.9, score=0.2),
        _product("milk", "纯牛奶整箱装", 39.9, score=0.95),
    ]

    ranked = filter_products(items, ShoppingSearchRequest(query="牛奶", top_k=5))
    comparison = compare_products(
        ranked,
        PriceCompareRequest(query="牛奶", items=ranked),
        provider="regression",
    )

    assert [item.product_id for item in ranked] == ["milk", "bath-gel"]
    assert comparison.comparison_status == "candidates_only"
    assert comparison.best_offer is None
    assert comparison.best_value_product_id is None
    assert "不能仅按价格" in comparison.summary


def test_verified_same_title_group_can_be_ranked_by_price() -> None:
    items = [
        _product("taobao-milk", "某品牌纯牛奶 250ml*24盒", 49.9),
        _product("jd-milk", "某品牌纯牛奶 250ml*24盒", 45.9, platform="jd"),
        _product("bath-gel", "牛奶香氛沐浴露", 9.9),
    ]

    comparison = compare_products(
        items,
        PriceCompareRequest(query="某品牌纯牛奶 250ml*24盒", items=items, sort_by="price"),
        provider="regression",
    )

    assert comparison.comparison_status == "comparable"
    assert comparison.best_offer is not None
    assert comparison.best_offer.product_id == "jd-milk"
