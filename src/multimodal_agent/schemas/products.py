"""Product search and price comparison schemas."""

from typing import Literal

from pydantic import BaseModel, Field


class ProductResult(BaseModel):
    """A candidate product returned by product search."""

    product_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    brand: str | None = None
    category: str | None = None
    price: float = Field(ge=0)
    currency: str = Field(default="CNY", min_length=1)
    platform: str = Field(min_length=1)
    shop: str | None = None
    url: str | None = None
    product_url: str | None = None
    image_url: str | None = None
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

    @property
    def success(self) -> bool:
        return not self.errors


class ProductSearchRequest(BaseModel):
    """Input for product search providers."""

    query: str | None = None
    visual_summary: str | None = None
    video_summary: str | None = None
    objects: list[str] = Field(default_factory=list)
    colors: list[str] = Field(default_factory=list)
    materials: list[str] = Field(default_factory=list)
    brand: str | None = None
    category: str | None = None
    budget_min: float | None = Field(default=None, ge=0)
    budget_max: float | None = Field(default=None, ge=0)
    budget: float | None = Field(default=None, ge=0)
    platforms: list[str] = Field(default_factory=list)
    top_k: int = Field(default=5, ge=1)
    user_id: str | None = None
    session_id: str | None = None
    memory_context: list[str] = Field(default_factory=list)


ProductSearchInput = ProductSearchRequest


class PriceOffer(BaseModel):
    """A normalized offer used by price comparison."""

    offer_id: str = Field(min_length=1)
    product_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    platform: str = Field(min_length=1)
    shop: str | None = None
    price: float = Field(ge=0)
    currency: str = Field(default="CNY", min_length=1)
    shipping_fee: float | None = Field(default=None, ge=0)
    total_price: float = Field(ge=0)
    product_url: str | None = None
    availability: str | None = None
    rating: float | None = Field(default=None, ge=0.0, le=5.0)
    sales: int | None = Field(default=None, ge=0)
    similarity_score: float | None = Field(default=None, ge=0.0, le=1.0)
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
    user_id: str | None = None
    session_id: str | None = None


PriceCompareInput = PriceCompareRequest
