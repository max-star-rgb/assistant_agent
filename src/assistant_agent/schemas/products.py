"""Product search and price comparison schemas."""

from typing import Literal

from pydantic import BaseModel, Field


ProductUrlStatus = Literal["unverified", "missing", "invalid_id", "verified", "unreachable"]
ProductAvailability = Literal["unknown", "available", "unavailable"]


class ProductResult(BaseModel):
    """A candidate product returned by product search."""

    product_id: str = Field(min_length=1)
    provider_item_id: str | None = None
    title: str = Field(min_length=1)
    brand: str | None = None
    category: str | None = None
    price: float = Field(ge=0)
    original_price: float | None = Field(default=None, ge=0)
    coupon_amount: float | None = Field(default=None, ge=0)
    effective_price: float | None = Field(default=None, ge=0)
    unconditional_price: float | None = Field(default=None, ge=0)
    conditional_price: float | None = Field(default=None, ge=0)
    conditional_price_note: str | None = None
    currency: str = Field(default="CNY", min_length=1)
    platform: str = Field(min_length=1)
    shop: str | None = None
    url: str | None = None
    product_url: str | None = None
    raw_url: str | None = None
    landing_url: str | None = None
    coupon_url: str | None = None
    click_url: str | None = None
    url_status: ProductUrlStatus | None = None
    availability: ProductAvailability | None = None
    image_url: str | None = None
    model: str | None = None
    specifications: dict[str, str] = Field(default_factory=dict)
    similarity: float | None = Field(default=None, ge=0.0, le=1.0)
    similarity_score: float | None = Field(default=None, ge=0.0, le=1.0)
    text_match_score: float | None = Field(default=None, ge=0.0, le=1.0)
    rating: float | None = Field(default=None, ge=0.0, le=5.0)
    sales: int | None = Field(default=None, ge=0)
    material: str | None = None
    color: str | None = None
    style_tags: list[str] = Field(default_factory=list)
    reason: str | None = None
    ranking_reason: "RankingReason | None" = None
    source: str = "mock"


class RankingReason(BaseModel):
    """Explainable ranking metadata shared by product search and price compare."""

    score: float = Field(ge=0.0, le=1.0)
    factors: dict[str, float] = Field(default_factory=dict)
    explanation: str = Field(min_length=1)


class ProductProviderError(BaseModel):
    """Structured product provider error."""

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    recoverable: bool = False


class ProductSearchResult(BaseModel):
    """Structured product search result returned by provider adapters."""

    items: list[ProductResult] = Field(default_factory=list)
    provider: str = Field(min_length=1)
    query_used: str | None = None
    filters_used: dict[str, object] = Field(default_factory=dict)
    total: int = Field(default=0, ge=0)
    errors: list[ProductProviderError] = Field(default_factory=list)
    latency_ms: int | None = Field(default=None, ge=0)
    output_ref: str | None = None
    requested_platforms: list[str] = Field(default_factory=list)
    succeeded_platforms: list[str] = Field(default_factory=list)
    failed_platforms: list[str] = Field(default_factory=list)
    platform_errors: dict[str, list[ProductProviderError]] = Field(default_factory=dict)

    @property
    def success(self) -> bool:
        return bool(self.items) or not self.errors


class ProductSearchRequest(BaseModel):
    """商品搜索 Provider 的输入。"""

    query: str = Field(
        min_length=1,
        description=(
            "用于 Provider 检索的简短商品核心关键词，例如“电脑双肩包”；"
            "不要把预算、使用场景和全部要求拼进关键词，预算和平台应填写对应结构化字段。"
        ),
    )
    visual_summary: str | None = None
    objects: list[str] = Field(default_factory=list)
    colors: list[str] = Field(default_factory=list)
    materials: list[str] = Field(default_factory=list)
    budget_min: float | None = Field(
        default=None,
        ge=0,
        description="用户明确给出的最低预算；未指定时省略。",
    )
    budget_max: float | None = Field(
        default=None,
        ge=0,
        description="用户明确给出的最高预算；未指定时省略。",
    )
    platforms: list[str] = Field(
        default_factory=list,
        description="用户明确指定的购物平台列表；未指定时省略。",
    )
    top_k: int = Field(default=5, ge=1)

class PriceOffer(BaseModel):
    """A normalized offer used by price comparison."""

    offer_id: str = Field(min_length=1)
    product_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    platform: str = Field(min_length=1)
    shop: str | None = None
    price: float = Field(ge=0)
    original_price: float | None = Field(default=None, ge=0)
    coupon_amount: float | None = Field(default=None, ge=0)
    effective_price: float | None = Field(default=None, ge=0)
    unconditional_price: float | None = Field(default=None, ge=0)
    conditional_price: float | None = Field(default=None, ge=0)
    conditional_price_note: str | None = None
    currency: str = Field(default="CNY", min_length=1)
    shipping_fee: float | None = Field(default=None, ge=0)
    total_price: float = Field(ge=0)
    product_url: str | None = None
    image_url: str | None = None
    url_status: ProductUrlStatus | None = None
    availability: ProductAvailability | None = None
    rating: float | None = Field(default=None, ge=0.0, le=5.0)
    sales: int | None = Field(default=None, ge=0)
    similarity_score: float | None = Field(default=None, ge=0.0, le=1.0)
    text_match_score: float | None = Field(default=None, ge=0.0, le=1.0)
    comparison_group: str | None = None
    same_product_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    data_completeness: float | None = Field(default=None, ge=0.0, le=1.0)
    reason: str | None = None
    ranking_reason: RankingReason | None = None


class PriceCompareResult(BaseModel):
    """Price comparison result for product candidates."""

    query: str = Field(min_length=1)
    items: list[ProductResult] = Field(default_factory=list)
    best_value_product_id: str | None = None
    summary: str = Field(min_length=1)
    offers: list[PriceOffer] = Field(default_factory=list)
    best_offer: PriceOffer | None = None
    ranking_reason: RankingReason | None = None
    comparison_status: Literal["comparable", "candidates_only"] = "candidates_only"
    provider: str = "mock"
    errors: list[ProductProviderError] = Field(default_factory=list)
    latency_ms: int | None = Field(default=None, ge=0)
    output_ref: str | None = None

    @property
    def success(self) -> bool:
        return not self.errors


class PriceCompareRequest(BaseModel):
    """Input for price comparison providers."""

    items: list[ProductResult] = Field(default_factory=list)
    query: str = "白色低帮运动鞋"
    budget_min: float | None = Field(default=None, ge=0)
    budget_max: float | None = Field(default=None, ge=0)
    platforms: list[str] = Field(default_factory=list)
    sort_by: Literal["price", "similarity", "rating", "value"] = "value"
    currency: str = "CNY"
    top_k: int = Field(default=5, ge=1)


class ShoppingSearchResult(BaseModel):
    """Combined shopping search and price comparison result."""

    query: str = Field(min_length=1)
    search: ProductSearchResult
    comparison: PriceCompareResult | None = None
    items: list[ProductResult] = Field(default_factory=list)
    offers: list[PriceOffer] = Field(default_factory=list)
    best_offer: PriceOffer | None = None
    best_value_product_id: str | None = None
    ranking_reason: RankingReason | None = None
    summary: str = Field(min_length=1)
    provider: str = "mock"
    errors: list[ProductProviderError] = Field(default_factory=list)
    latency_ms: int | None = Field(default=None, ge=0)
    output_ref: str | None = None

    @property
    def success(self) -> bool:
        return self.comparison is not None and self.comparison.success
