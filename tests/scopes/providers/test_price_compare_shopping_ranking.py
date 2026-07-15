from assistant_agent.schemas.products import PriceCompareRequest, ProductResult
from assistant_agent.utils.product_matching import compare_products


def _product(product_id: str, platform: str, price: float, title: str, **kwargs) -> ProductResult:
    return ProductResult(
        product_id=product_id,
        title=title,
        platform=platform,
        price=price,
        product_url=f"https://{platform}.example/{product_id}",
        image_url=f"https://img.example/{product_id}.jpg",
        **kwargs,
    )


def test_compare_prefers_same_spec_and_unconditional_price_with_platform_quotas() -> None:
    items = [
        _product("jd-best", "jd", 2999, "示例 X 16GB 512GB", brand="示例", model="X", specifications={"memory": "16GB", "storage": "512GB"}, sales=10),
        _product("pdd-member", "pdd", 2799, "示例 X 16GB 512GB 会员价", brand="示例", model="X", specifications={"memory": "16GB", "storage": "512GB"}, conditional_price=2799, unconditional_price=3099, sales=1000),
        _product("tb-other", "taobao", 2899, "示例 X 8GB 256GB", brand="示例", model="X", specifications={"memory": "8GB", "storage": "256GB"}, sales=5000),
        *[_product(f"jd-{index}", "jd", 3100 + index, f"示例 X 16GB 512GB {index}", brand="示例", model="X", specifications={"memory": "16GB", "storage": "512GB"}) for index in range(4)],
    ]

    result = compare_products(items, PriceCompareRequest(query="示例 X 16GB 512GB", top_k=20), provider="test")

    assert result.best_offer is not None
    assert result.best_offer.product_id == "jd-best"
    assert sum(offer.platform == "jd" for offer in result.offers) == 3
    assert len(result.offers) <= 9
    conditional = next(offer for offer in result.offers if offer.product_id == "pdd-member")
    assert conditional.total_price == 3099
    assert conditional.conditional_price == 2799
    assert next(offer for offer in result.offers if offer.product_id == "tb-other").comparison_group != result.best_offer.comparison_group


def test_value_ranking_uses_link_availability_then_data_completeness() -> None:
    missing = _product(
        "missing",
        "jd",
        100,
        "示例 X",
        url_status="missing",
        brand="示例",
        model="X",
        specifications={"storage": "512GB"},
    )
    complete = _product(
        "complete",
        "taobao",
        100,
        "示例 X",
        url_status="unverified",
        brand="示例",
        model="X",
        specifications={"storage": "512GB"},
        original_price=120,
    )

    result = compare_products(
        [missing, complete],
        PriceCompareRequest(query="示例 X 512GB", top_k=9),
        provider="test",
    )

    assert [offer.product_id for offer in result.offers] == ["complete", "missing"]
