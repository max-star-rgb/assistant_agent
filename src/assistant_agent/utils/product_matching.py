"""Pure product matching and price-comparison helpers shared by adapters."""

from collections.abc import Sequence
from dataclasses import dataclass

from assistant_agent.schemas.products import (
    PriceCompareRequest,
    PriceCompareResult,
    PriceOffer,
    ProductProviderError,
    ProductResult,
    ProductSearchRequest,
    ProductSearchResult,
    RankingReason,
)


@dataclass(frozen=True)
class ProviderLimit:
    """Provider-specific page-size mapping for a provider-neutral top_k request."""

    requested: int
    provider_limit: int
    normalized: bool
    capped: bool = False


def normalize_provider_limit(
    requested: int | None,
    *,
    default: int,
    allowed_values: Sequence[int] | None = None,
    max_value: int | None = None,
) -> ProviderLimit:
    """Map a provider-neutral top_k value onto provider-supported page sizes."""

    requested_limit = requested or default
    if allowed_values:
        allowed = sorted({value for value in allowed_values if value >= 1})
        if not allowed:
            raise ValueError("allowed_values must contain at least one positive integer.")
        provider_limit = next((value for value in allowed if value >= requested_limit), allowed[-1])
        return ProviderLimit(
            requested=requested_limit,
            provider_limit=provider_limit,
            normalized=provider_limit != requested_limit,
            capped=requested_limit > allowed[-1],
        )

    provider_limit = min(requested_limit, max_value) if max_value is not None else requested_limit
    return ProviderLimit(
        requested=requested_limit,
        provider_limit=provider_limit,
        normalized=provider_limit != requested_limit,
        capped=max_value is not None and requested_limit > max_value,
    )


def filter_products(items: list[ProductResult], request: ProductSearchRequest) -> list[ProductResult]:
    """Filter and rank product candidates for a search request."""

    filtered = items
    if request.platforms:
        platforms = set(request.platforms)
        filtered = [item for item in filtered if item.platform in platforms]
    if request.brand:
        brand = request.brand.lower()
        filtered = [item for item in filtered if brand in item.title.lower()]
    max_budget = request.budget_max if request.budget_max is not None else request.budget
    if max_budget is not None:
        filtered = [item for item in filtered if item.price <= max_budget]

    tokens = query_tokens(request)
    if tokens:
        matched = [
            item
            for item in filtered
            if any(token in f"{item.title} {item.reason or ''} {item.platform}".lower() for token in tokens)
        ]
        if len(matched) >= 2 or len(filtered) == 1:
            filtered = matched

    return filtered[: request.top_k]


def query_text(request: ProductSearchRequest) -> str:
    """Build a provider-neutral product query string."""

    parts = [
        request.query,
        request.visual_summary,
        request.video_summary,
        " ".join(request.objects),
        " ".join(request.colors),
        " ".join(request.materials),
        request.category,
    ]
    return " ".join(part.strip() for part in parts if part and part.strip())


def query_tokens(request: ProductSearchRequest) -> list[str]:
    """Tokenize a product query for deterministic local matching."""

    text = query_text(request).lower()
    return [token for token in text.replace("/", " ").split() if len(token) >= 2]


def filters_used(request: ProductSearchRequest) -> dict[str, object]:
    """Return non-empty filters applied to a product search."""

    filters: dict[str, object] = {}
    for key in ("brand", "category", "budget_min", "budget_max", "budget", "platforms", "top_k"):
        value = getattr(request, key)
        if value not in (None, [], ""):
            filters[key] = value
    return filters


def failed_search_result(
    provider: str,
    code: str,
    message: str,
    recoverable: bool,
) -> ProductSearchResult:
    """Build a structured product-search failure."""

    return ProductSearchResult(
        provider=provider,
        errors=[ProductProviderError(code=code, message=message, recoverable=recoverable)],
        total=0,
    )


def compare_products(
    items: list[ProductResult],
    request: PriceCompareRequest,
    *,
    provider: str,
    output_ref: str | None = None,
    latency_ms: int | None = None,
) -> PriceCompareResult:
    """Rank product candidates into a structured price comparison result."""

    if not items:
        return failed_price_result(
            provider=provider,
            query=request.query,
            code="price_no_products",
            message="缺少商品候选列表，无法比价",
            recoverable=True,
        )

    offers = [offer_from_product(item) for item in items if item.price is not None]
    offers = filter_offers(offers, request)
    if not offers:
        return failed_price_result(
            provider=provider,
            query=request.query,
            code="price_no_offers",
            message="没有符合预算或平台条件的报价。",
            recoverable=True,
        )

    offers = sort_offers(offers, request.sort_by)[: request.top_k]
    best_offer = offers[0]
    items_by_id = {item.product_id: item for item in items}
    sorted_items = [items_by_id[offer.product_id] for offer in offers if offer.product_id in items_by_id]
    reason = ranking_reason(best_offer, request.sort_by)
    return PriceCompareResult(
        query=request.query,
        items=sorted_items,
        best_value_product_id=best_offer.product_id,
        summary=f"{best_offer.title} 当前综合最优，总价 {best_offer.total_price:.2f} {best_offer.currency}。",
        offers=offers,
        best_offer=best_offer,
        ranking_reason=reason,
        provider=provider,
        latency_ms=latency_ms,
        output_ref=output_ref,
    )


def offer_from_product(product: ProductResult) -> PriceOffer:
    """Build a normalized price offer from a product candidate."""

    shipping_fee = 0.0
    total_price = product.price + shipping_fee
    return PriceOffer(
        offer_id=f"offer_{product.product_id}_{product.platform}",
        product_id=product.product_id,
        title=product.title,
        platform=product.platform,
        shop=product.shop,
        price=product.price,
        currency=product.currency,
        shipping_fee=shipping_fee,
        total_price=total_price,
        product_url=product.product_url or product.url,
        url_status=product.url_status,
        availability=product.availability or "unknown",
        rating=product.rating,
        sales=product.sales,
        similarity_score=product.similarity_score if product.similarity_score is not None else product.similarity,
        reason=product.reason,
        ranking_reason=product.ranking_reason,
    )


def filter_offers(offers: list[PriceOffer], request: PriceCompareRequest) -> list[PriceOffer]:
    """Apply platform and budget filters to offers."""

    filtered = offers
    if request.platforms:
        platforms = set(request.platforms)
        filtered = [offer for offer in filtered if offer.platform in platforms]
    if request.budget_min is not None:
        filtered = [offer for offer in filtered if offer.total_price >= request.budget_min]
    if request.budget_max is not None:
        filtered = [offer for offer in filtered if offer.total_price <= request.budget_max]
    return filtered


def sort_offers(offers: list[PriceOffer], sort_by: str) -> list[PriceOffer]:
    """Sort offers according to the requested comparison mode."""

    if sort_by == "price":
        return sorted(offers, key=lambda offer: offer.total_price)
    if sort_by == "similarity":
        return sorted(offers, key=lambda offer: (offer.similarity_score or 0.0, -offer.total_price), reverse=True)
    if sort_by == "rating":
        return sorted(offers, key=lambda offer: (offer.rating or 0.0, -offer.total_price), reverse=True)
    return sorted(
        offers,
        key=lambda offer: (
            offer.total_price,
            -(offer.similarity_score or 0.0),
            -(offer.rating or 0.0),
        ),
    )


def ranking_reason(best_offer: PriceOffer, sort_by: str) -> RankingReason:
    """Explain why the best offer was selected."""

    if sort_by == "price":
        explanation = f"{best_offer.title} 总价最低。"
        factors = {"price": 1.0}
        score = 0.9
    elif sort_by == "similarity":
        explanation = f"{best_offer.title} 相似度最高且价格可比较。"
        factors = {"visual_similarity": best_offer.similarity_score or 0.0, "price": 0.8}
        score = max(best_offer.similarity_score or 0.0, 0.8)
    elif sort_by == "rating":
        explanation = f"{best_offer.title} 评分最高且价格可比较。"
        factors = {"rating": (best_offer.rating or 0.0) / 5.0, "price": 0.8}
        score = max((best_offer.rating or 0.0) / 5.0, 0.8)
    else:
        explanation = f"{best_offer.title} 在价格、相似度和评分之间综合最优。"
        factors = {
            "price": 1.0,
            "visual_similarity": best_offer.similarity_score or 0.0,
            "rating": (best_offer.rating or 0.0) / 5.0,
        }
        score = min(1.0, max(factors.values()))
    return RankingReason(score=score, factors=factors, explanation=explanation)


def failed_price_result(
    provider: str,
    query: str,
    code: str,
    message: str,
    recoverable: bool,
) -> PriceCompareResult:
    """Build a structured price-comparison failure."""

    return PriceCompareResult(
        query=query or "price_compare",
        summary=message,
        provider=provider,
        errors=[ProductProviderError(code=code, message=message, recoverable=recoverable)],
    )
