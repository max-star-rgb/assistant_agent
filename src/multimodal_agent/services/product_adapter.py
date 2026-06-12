"""Product search and price comparison adapter interfaces."""

from typing import Protocol

from pydantic import BaseModel, Field

from multimodal_agent.schemas.products import PriceCompareResult, ProductResult


class ProductSearchInput(BaseModel):
    """Input for product search."""

    query: str | None = None
    visual_summary: str | None = None
    budget: float | None = None
    brand: str | None = None
    platforms: list[str] = Field(default_factory=list)


class PriceCompareInput(BaseModel):
    """Input for price comparison."""

    items: list[ProductResult] = Field(default_factory=list)
    query: str = "白色低帮运动鞋"


class ProductSearchAdapter(Protocol):
    """Adapter contract for product search and price comparison providers."""

    def search(self, input: ProductSearchInput) -> list[ProductResult]:
        """Return product candidates."""

    def compare(self, input: PriceCompareInput) -> PriceCompareResult:
        """Return products sorted and summarized by price."""


class MockProductSearchAdapter:
    """Deterministic local adapter for product search and comparison."""

    def search(self, input: ProductSearchInput) -> list[ProductResult]:
        if not input.query and not input.visual_summary:
            raise ValueError("缺少商品描述，无法搜索")

        return [
            ProductResult(
                product_id="p1",
                title="白色低帮运动鞋 A",
                price=299.0,
                platform="mock-shop-a",
                url="mock://shop-a/p1",
                similarity=0.92,
                rating=4.7,
                reason="颜色、鞋型和材质相似度最高",
            ),
            ProductResult(
                product_id="p2",
                title="简约白色板鞋 B",
                price=259.0,
                platform="mock-shop-b",
                url="mock://shop-b/p2",
                similarity=0.86,
                rating=4.5,
                reason="价格更低，外观接近",
            ),
            ProductResult(
                product_id="p3",
                title="日系白色休闲鞋 C",
                price=339.0,
                platform="mock-shop-c",
                url="mock://shop-c/p3",
                similarity=0.81,
                rating=4.8,
                reason="风格接近，评分较高",
            ),
        ]

    def compare(self, input: PriceCompareInput) -> PriceCompareResult:
        if not input.items:
            raise ValueError("缺少商品候选列表，无法比价")

        sorted_items = sorted(input.items, key=lambda item: item.price)
        return PriceCompareResult(
            query=input.query,
            items=sorted_items,
            best_value_product_id=sorted_items[0].product_id,
            summary=f"{sorted_items[0].title} 当前价格最低，为 {sorted_items[0].price:.2f}。",
        )
