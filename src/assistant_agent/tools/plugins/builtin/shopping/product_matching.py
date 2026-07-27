"""Pure product matching and price-comparison helpers shared by adapters."""

from collections.abc import Sequence
from dataclasses import dataclass

from assistant_agent.tools.plugins.builtin.shopping.models import (
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
    if request.budget_max is not None:
        filtered = [item for item in filtered if item.price <= request.budget_max]

    tokens = query_tokens(request)
    if tokens:
        matched = [
            item
            for item in filtered
            if any(token in f"{item.title} {item.reason or ''} {item.platform}".lower() for token in tokens)
        ]
        if len(matched) >= 2 or len(filtered) == 1:
            filtered = matched

    indexed = list(enumerate(filtered))
    indexed.sort(key=lambda pair: (-_product_relevance(pair[1], request), pair[0]))
    return [item for _, item in indexed[: request.top_k]]


def query_text(request: ProductSearchRequest) -> str:
    """Build a provider-neutral product query string."""

    parts = [
        request.query,
        request.visual_summary,
        " ".join(request.objects),
        " ".join(request.colors),
        " ".join(request.materials),
    ]
    return " ".join(part.strip() for part in parts if part and part.strip())


def query_tokens(request: ProductSearchRequest) -> list[str]:
    """Tokenize a product query for deterministic local matching."""

    text = query_text(request).lower()
    return [token for token in text.replace("/", " ").split() if len(token) >= 2]


def filters_used(request: ProductSearchRequest) -> dict[str, object]:
    """Return non-empty filters applied to a product search."""

    filters: dict[str, object] = {}
    for key in ("budget_min", "budget_max", "platforms", "top_k"):
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

    offers = [offer_from_product(item, query=request.query) for item in items if item.price is not None]
    offers = filter_offers(offers, request)
    if not offers:
        return failed_price_result(
            provider=provider,
            query=request.query,
            code="price_no_offers",
            message="没有符合预算或平台条件的报价。",
            recoverable=True,
        )

    offers = _apply_platform_quotas(offers, top_k=request.top_k)
    items_by_id = {item.product_id: item for item in items}
    sorted_items = [items_by_id[offer.product_id] for offer in offers if offer.product_id in items_by_id]
    comparable = _largest_comparable_group(offers)
    if not comparable:
        return PriceCompareResult(
            query=request.query,
            items=sorted_items,
            summary=(
                f"已找到 {len(offers)} 个相关商品候选，但缺少可验证的同商品身份，"
                "不能仅按价格选出最优商品。"
            ),
            offers=offers,
            comparison_status="candidates_only",
            provider=provider,
            latency_ms=latency_ms,
            output_ref=output_ref,
        )

    comparable = sort_offers(comparable, request.sort_by)
    best_offer = comparable[0]
    reason = ranking_reason(best_offer, request.sort_by)
    return PriceCompareResult(
        query=request.query,
        items=sorted_items,
        best_value_product_id=best_offer.product_id,
        summary=f"{best_offer.title} 当前综合最优，总价 {best_offer.total_price:.2f} {best_offer.currency}。",
        offers=offers,
        best_offer=best_offer,
        ranking_reason=reason,
        comparison_status="comparable",
        provider=provider,
        latency_ms=latency_ms,
        output_ref=output_ref,
    )


def offer_from_product(product: ProductResult, *, query: str = "") -> PriceOffer:
    """Build a normalized price offer from a product candidate."""

    shipping_fee = 0.0
    unconditional_price = product.unconditional_price if product.unconditional_price is not None else product.price
    total_price = unconditional_price + shipping_fee
    comparison_group = _comparison_group(product)
    return PriceOffer(
        offer_id=f"offer_{product.product_id}_{product.platform}",
        product_id=product.product_id,
        title=product.title,
        platform=product.platform,
        shop=product.shop,
        price=product.price,
        original_price=product.original_price,
        coupon_amount=product.coupon_amount,
        effective_price=product.effective_price or product.price,
        unconditional_price=unconditional_price,
        conditional_price=product.conditional_price,
        conditional_price_note=product.conditional_price_note,
        currency=product.currency,
        shipping_fee=shipping_fee,
        total_price=total_price,
        product_url=product.product_url or product.url,
        image_url=product.image_url,
        url_status=product.url_status,
        availability=product.availability or "unknown",
        rating=product.rating,
        sales=product.sales,
        similarity_score=product.similarity_score if product.similarity_score is not None else product.similarity,
        text_match_score=(
            product.text_match_score
            if product.text_match_score is not None
            else _query_title_relevance(query, product.title)
        ),
        comparison_group=comparison_group,
        same_product_confidence=_same_product_confidence(product, query),
        data_completeness=_data_completeness(product),
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
        return sorted(
            offers,
            key=lambda offer: (
                -(offer.same_product_confidence or 0.0),
                offer.total_price,
                _link_rank(offer),
                -(offer.sales or 0),
                -(offer.data_completeness or 0.0),
            ),
        )
    if sort_by == "similarity":
        return sorted(offers, key=lambda offer: (offer.similarity_score or 0.0, -offer.total_price), reverse=True)
    if sort_by == "rating":
        return sorted(offers, key=lambda offer: (offer.rating or 0.0, -offer.total_price), reverse=True)
    return sorted(
        offers,
        key=lambda offer: (
            -(offer.same_product_confidence or 0.0),
            offer.total_price,
            _link_rank(offer),
            -(offer.sales or 0),
            -(offer.data_completeness or 0.0),
            -(offer.similarity_score or 0.0),
            -(offer.rating or 0.0),
        ),
    )


def _comparison_group(product: ProductResult) -> str | None:
    if product.model or product.specifications:
        parts = [product.brand or "", product.model or ""]
        parts.extend(f"{key}={value}" for key, value in sorted(product.specifications.items()))
        identity = "|".join(part.strip().lower() for part in parts if part.strip())
        if identity:
            return f"identity:{identity}"
    title = _normalized_identity_text(product.title)
    return f"title:{title}" if title else None


def _link_rank(offer: PriceOffer) -> int:
    if offer.url_status == "verified":
        return 0
    if offer.url_status == "unverified" and offer.product_url:
        return 1
    return 2


def _data_completeness(product: ProductResult) -> float:
    values = (
        product.brand,
        product.model,
        product.original_price,
        product.image_url,
        product.sales,
        product.product_url or product.url,
        product.specifications or None,
    )
    return sum(value is not None for value in values) / len(values)


def _same_product_confidence(product: ProductResult, query: str) -> float:
    query_lower = query.lower()
    identity = [product.brand, product.model, *product.specifications.values()]
    present = [value for value in identity if value and value.strip()]
    if not present:
        # Search relevance and visual similarity cannot prove an identical SKU.
        return 0.0
    matched = sum(str(value).lower() in query_lower for value in present)
    return matched / len(present)


def _apply_platform_quotas(offers: list[PriceOffer], *, top_k: int) -> list[PriceOffer]:
    selected: list[PriceOffer] = []
    counts: dict[str, int] = {}
    for offer in offers:
        platform = "taobao" if offer.platform == "tmall" else offer.platform
        if counts.get(platform, 0) >= 3:
            continue
        selected.append(offer)
        counts[platform] = counts.get(platform, 0) + 1
        if len(selected) >= min(top_k, 9):
            break
    return selected


def _largest_comparable_group(offers: list[PriceOffer]) -> list[PriceOffer]:
    groups: dict[str, list[PriceOffer]] = {}
    for offer in offers:
        if not offer.comparison_group:
            continue
        groups.setdefault(offer.comparison_group, []).append(offer)
    candidates = [group for group in groups.values() if len(group) >= 2]
    if not candidates:
        return []
    return max(
        candidates,
        key=lambda group: (
            sum(item.text_match_score or 0.0 for item in group) / len(group),
            len(group),
            max(item.same_product_confidence or 0.0 for item in group),
        ),
    )


def _product_relevance(product: ProductResult, request: ProductSearchRequest) -> float:
    if product.text_match_score is not None:
        return product.text_match_score
    query = _normalized_identity_text(query_text(request))
    haystack = _normalized_identity_text(" ".join((product.title, product.reason or "")))
    if not query or not haystack:
        return 0.0
    if query in haystack:
        return 1.0
    tokens = query_tokens(request)
    if not tokens:
        return 0.0
    return sum(_normalized_identity_text(token) in haystack for token in tokens) / len(tokens)


def _normalized_identity_text(value: str) -> str:
    return "".join(character.lower() for character in value if character.isalnum())


def _query_title_relevance(query: str, title: str) -> float:
    normalized_query = _normalized_identity_text(query)
    normalized_title = _normalized_identity_text(title)
    if not normalized_query or not normalized_title:
        return 0.0
    if normalized_query in normalized_title:
        return 1.0
    return 0.0


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
        query=query or "shopping_search",
        summary=message,
        provider=provider,
        errors=[ProductProviderError(code=code, message=message, recoverable=recoverable)],
    )
