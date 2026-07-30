"""Product search and price comparison schemas."""

from typing import Literal

from pydantic import BaseModel, Field, model_validator


ProductUrlStatus = Literal["unverified", "missing", "invalid_id", "verified", "unreachable"]
ProductAvailability = Literal["unknown", "available", "unavailable"]
ShoppingSearchOutcome = Literal["success", "partial", "empty", "failed"]
ShoppingListNeedStatus = Literal["selected", "empty", "failed", "budget_excluded"]


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
    source: str = Field(default="unknown", min_length=1)


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
    """Plugin-private single-keyword request passed to Provider adapters."""

    query: str = Field(
        min_length=1,
        description="商品、特征、场景及平台等检索需求。",
    )
    budget_min: float | None = Field(
        default=None,
        ge=0,
        description="用户明确给出的最低预算。",
    )
    budget_max: float | None = Field(
        default=None,
        ge=0,
        description="用户明确给出的最高预算。",
    )
    platforms: list[str] = Field(
        default_factory=list,
        description="用户明确指定的购物平台列表；未指定时省略。",
    )
    top_k: int = Field(default=5, ge=1)


class ShoppingListNeed(BaseModel):
    """购物清单中需要独立搜索的一个商品品类。"""

    keyword: str = Field(
        min_length=1,
        description="单一商品品类及必要特征；不得拼接多个互不相干的商品品类。",
    )
    quantity: int = Field(
        default=1,
        ge=1,
        le=20,
        description="需要购买的数量。",
    )
    required: bool = Field(
        default=True,
        description="总预算不足时是否优先覆盖该清单项。",
    )
    max_unit_price: float | None = Field(
        default=None,
        ge=0,
        description="该清单项的单件价格上限；不是整份清单的总预算。",
    )


class ShoppingEvidence(BaseModel):
    """解释清单选择原因的结构化前序工具证据。"""

    source_tool: str = Field(
        min_length=1,
        description="产生证据的工具名，例如 weather。",
    )
    output_ref: str | None = Field(
        default=None,
        description="前序工具输出引用；没有引用时省略。",
    )
    summary: str = Field(
        min_length=1,
        description="与当前购物决策直接相关的证据摘要。",
    )


class ShoppingSearchRequest(BaseModel):
    """统一购物请求；单品和多品类都通过 needs 表达。"""

    scenario: str | None = Field(
        default=None,
        min_length=1,
        description="清单服务的具体场景，例如室内聚餐或雨天通勤。",
    )
    decision_reason: str | None = Field(
        default=None,
        min_length=1,
        description="选择这些商品品类的原因；如由天气决定，应明确说明判断。",
    )
    evidence: list[ShoppingEvidence] = Field(
        default_factory=list,
        description="支持场景判断的结构化工具证据，例如 weather 的摘要和 output_ref。",
    )
    total_budget: float | None = Field(
        default=None,
        gt=0,
        description="整份清单的总预算；多于一个 need 时必填。",
    )
    needs: list[ShoppingListNeed] = Field(
        min_length=1,
        max_length=8,
        description="需要分别搜索的商品清单项，最多八项。",
    )
    platforms: list[str] = Field(
        default_factory=list,
        description="用户明确指定的购物平台列表；未指定时省略。",
    )
    top_k_per_need: int = Field(default=3, ge=1, le=5)

    @model_validator(mode="after")
    def validate_request(self) -> "ShoppingSearchRequest":
        normalized = [" ".join(need.keyword.split()).casefold() for need in self.needs]
        if len(normalized) != len(set(normalized)):
            raise ValueError("needs must use distinct product keywords")
        if len(self.needs) > 1 and self.total_budget is None:
            raise ValueError("total_budget is required when needs contains multiple items")
        return self


class ShoppingListSelection(BaseModel):
    """A product selected for one list need."""

    keyword: str
    quantity: int = Field(ge=1)
    product: ProductResult
    unit_price: float = Field(ge=0)
    subtotal: float = Field(ge=0)


class ShoppingListNeedResult(BaseModel):
    """Search evidence and basket decision for one requested need."""

    need: ShoppingListNeed
    status: ShoppingListNeedStatus
    query_used: str | None = None
    candidates: list[ProductResult] = Field(default_factory=list)
    selected: ShoppingListSelection | None = None
    errors: list[ProductProviderError] = Field(default_factory=list)


class ShoppingSearchResult(BaseModel):
    """Unified single- and multi-need shopping result."""

    outcome: ShoppingSearchOutcome
    scenario: str | None = None
    decision_reason: str | None = None
    evidence: list[ShoppingEvidence] = Field(default_factory=list)
    total_budget: float | None = Field(default=None, gt=0)
    total_cost: float = Field(ge=0)
    within_budget: bool
    needs: list[ShoppingListNeedResult]
    selections: list[ShoppingListSelection] = Field(default_factory=list)
    uncovered_required_needs: list[str] = Field(default_factory=list)
    summary: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    errors: list[ProductProviderError] = Field(default_factory=list)
    latency_ms: int | None = Field(default=None, ge=0)
    output_refs: list[str] = Field(default_factory=list)

    @property
    def success(self) -> bool:
        return self.outcome != "failed"


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
    provider: str = Field(min_length=1)
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
