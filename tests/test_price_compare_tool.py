from assistant_agent.schemas.products import ProductResult
from assistant_agent.services.product_adapter import MockPriceCompareAdapter
from assistant_agent.tools.price_compare_tool import PriceCompareTool


def test_price_compare_tool_returns_offers_and_best_offer() -> None:
    result = PriceCompareTool(adapter=MockPriceCompareAdapter()).run(
        {
            "query": "白色运动鞋",
            "items": [
                ProductResult(
                    product_id="p1",
                    title="白色低帮运动鞋 A",
                    price=299,
                    platform="mock-shop-a",
                    similarity=0.92,
                ).model_dump(),
                ProductResult(
                    product_id="p2",
                    title="简约白色板鞋 B",
                    price=259,
                    platform="mock-shop-b",
                    similarity=0.86,
                ).model_dump(),
            ],
        }
    )

    assert result.success is True
    assert result.data["offers"][0]["product_id"] == "p2"
    assert result.data["best_offer"]["product_id"] == "p2"
    assert result.output_ref == "mock://compare/white-low-top-sneaker"


def test_price_compare_tool_returns_structured_error_without_products() -> None:
    result = PriceCompareTool(adapter=MockPriceCompareAdapter()).run({"items": []})

    assert result.success is False
    assert result.error == "缺少商品候选列表，无法比价"
    assert result.data["errors"][0]["code"] == "price_no_products"
