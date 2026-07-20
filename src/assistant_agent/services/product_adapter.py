"""Product search and price comparison adapter interfaces."""

import json
from pathlib import Path
from typing import Any, Protocol

from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.products import (
    PriceOffer,
    PriceCompareResult,
    PriceCompareInput,
    PriceCompareRequest,
    ProductProviderError,
    ProductResult,
    ProductSearchInput,
    ProductSearchRequest,
    ProductSearchResult,
    RankingReason,
)
from assistant_agent.services.provider_errors import build_provider_error
from assistant_agent.services.tool_manifest import SHOPPING_SEARCH_CAPABILITY
from assistant_agent.utils.product_matching import compare_products


class ProductSearchAdapter(Protocol):
    """Adapter contract for product search providers."""

    def search(self, request: ProductSearchRequest) -> ProductSearchResult:
        """Return structured product candidates."""


class PriceCompareAdapter(Protocol):
    """Adapter contract for price comparison providers."""

    def compare(self, request: PriceCompareRequest) -> PriceCompareResult:
        """Return structured price offers."""


ShoppingSearchAdapter = ProductSearchAdapter
ShoppingCompareAdapter = PriceCompareAdapter


class MockProductSearchAdapter:
    """Deterministic local adapter for product search and comparison."""

    provider = "mock"

    def search(self, request: ProductSearchRequest) -> ProductSearchResult:
        if not _query_text(request):
            return _failed_search_result(
                provider=self.provider,
                code="product_query_empty",
                message="缺少商品描述，无法搜索",
                recoverable=True,
            )

        items = _filter_products(_mock_products(), request)
        return ProductSearchResult(
            items=items,
            provider=self.provider,
            query_used=_query_text(request),
            filters_used=_filters_used(request),
            total=len(items),
            latency_ms=1,
            output_ref="mock://products/white-low-top-sneaker",
        )

    def compare(self, input: PriceCompareRequest) -> PriceCompareResult:
        return MockPriceCompareAdapter().compare(input)


class MockPriceCompareAdapter:
    """Deterministic local price comparison adapter."""

    provider = "mock"

    def compare(self, request: PriceCompareRequest) -> PriceCompareResult:
        return compare_products(
            request.items,
            request,
            provider=self.provider,
            output_ref="mock://compare/white-low-top-sneaker",
            latency_ms=1,
        )


class LocalPriceCompareAdapter(MockPriceCompareAdapter):
    """Local deterministic price comparison adapter for offline demos."""

    provider = "local"


class HttpPriceCompareAdapter:
    """HTTP shopping compare skeleton. It never performs network IO in Phase 5C Task 057."""

    provider = "http"

    def __init__(
        self,
        base_url: str | None,
        api_key: str | None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def compare(self, request: PriceCompareRequest) -> PriceCompareResult:
        missing = []
        if not self.base_url:
            missing.append("SHOPPING_COMPARE_BASE_URL")
        if not self.api_key:
            missing.append("SHOPPING_COMPARE_API_KEY")
        if missing:
            return _failed_price_result(
                provider=self.provider,
                query=request.query,
                code="provider_unconfigured",
                message=f"http shopping compare provider is missing {', '.join(missing)}.",
                recoverable=True,
            )
        return _failed_price_result(
            provider=self.provider,
            query=request.query,
            code="provider_unavailable",
            message="http shopping compare provider is a Phase 5C skeleton and does not perform network IO.",
            recoverable=False,
        )


class LocalJsonProductSearchAdapter:
    """Local JSON product search adapter for offline demos."""

    provider = "local_json"

    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path) if path else None

    def search(self, request: ProductSearchRequest) -> ProductSearchResult:
        if self.path is None:
            return _failed_search_result(
                provider=self.provider,
                code="provider_unconfigured",
                message="local_json shopping search provider is missing SHOPPING_SEARCH_LOCAL_PATH.",
                recoverable=True,
            )
        if not self.path.exists():
            return _failed_search_result(
                provider=self.provider,
                code="provider_unconfigured",
                message="local_json product search data file does not exist.",
                recoverable=True,
            )
        if not _query_text(request):
            return _failed_search_result(
                provider=self.provider,
                code="product_query_empty",
                message="缺少商品描述，无法搜索",
                recoverable=True,
            )

        try:
            items = _load_local_products(self.path)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            return _failed_search_result(
                provider=self.provider,
                code="provider_bad_response",
                message=f"local_json product data is invalid: {exc}",
                recoverable=False,
            )

        filtered = _filter_products(items, request)
        return ProductSearchResult(
            items=filtered,
            provider=self.provider,
            query_used=_query_text(request),
            filters_used=_filters_used(request),
            total=len(filtered),
            latency_ms=1,
            output_ref="local://products/search",
        )


class HttpProductSearchAdapter:
    """HTTP shopping search skeleton. It never performs network IO in Phase 5C Task 056."""

    provider = "http"

    def __init__(
        self,
        base_url: str | None,
        api_key: str | None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def search(self, request: ProductSearchRequest) -> ProductSearchResult:
        missing = []
        if not self.base_url:
            missing.append("SHOPPING_SEARCH_BASE_URL")
        if not self.api_key:
            missing.append("SHOPPING_SEARCH_API_KEY")
        if missing:
            return _failed_search_result(
                provider=self.provider,
                code="provider_unconfigured",
                message=f"http shopping search provider is missing {', '.join(missing)}.",
                recoverable=True,
            )
        return _failed_search_result(
            provider=self.provider,
            code="provider_unavailable",
            message="http shopping search provider is a Phase 5C skeleton and does not perform network IO.",
            recoverable=False,
        )


class UnconfiguredProductSearchAdapter:
    """Adapter returned when a selected shopping search provider lacks configuration."""

    def __init__(self, provider: str, missing: str) -> None:
        self.provider = provider
        self.missing = missing

    def search(self, request: ProductSearchRequest) -> ProductSearchResult:
        return _failed_search_result(
            provider=self.provider,
            code="provider_unconfigured",
            message=f"{self.provider} shopping search provider is missing {self.missing}.",
            recoverable=True,
        )


def create_shopping_search_adapter(config: ProviderConfig | None = None) -> ShoppingSearchAdapter:
    """Create a shopping search adapter without initializing real provider clients."""

    resolved = config or ProviderConfig.from_env()
    if resolved.shopping_search_provider == "local_json":
        if not resolved.shopping_search_local_path:
            return UnconfiguredProductSearchAdapter("local_json", "SHOPPING_SEARCH_LOCAL_PATH")
        return LocalJsonProductSearchAdapter(resolved.shopping_search_local_path)
    if resolved.shopping_search_provider == "haodanku":
        if not resolved.haodanku_api_key:
            return UnconfiguredProductSearchAdapter("haodanku", "HAODANKU_API_KEY")
        from assistant_agent.providers.haodanku_product_search import (
            HaodankuConfig,
            HaodankuProductSearchAdapter,
        )

        return HaodankuProductSearchAdapter(
            HaodankuConfig(
                api_key=resolved.haodanku_api_key,
                base_url=resolved.haodanku_base_url,
                timeout_seconds=resolved.haodanku_timeout_seconds,
                enabled_platforms=resolved.haodanku_enabled_platforms,
                taobao_pid=resolved.haodanku_taobao_pid,
                taobao_authorized_name=resolved.haodanku_taobao_authorized_name,
                jd_sub_union_id=resolved.haodanku_jd_sub_union_id,
                pdd_channel=resolved.haodanku_pdd_channel,
            )
        )
    if resolved.shopping_search_provider == "http":
        return HttpProductSearchAdapter(
            base_url=resolved.shopping_search_base_url,
            api_key=resolved.shopping_search_api_key,
            timeout_seconds=resolved.shopping_search_timeout_seconds,
        )
    return MockProductSearchAdapter()


def create_shopping_compare_adapter(config: ProviderConfig | None = None) -> ShoppingCompareAdapter:
    """Create a shopping compare adapter without initializing real provider clients."""

    resolved = config or ProviderConfig.from_env()
    if resolved.shopping_compare_provider == "local":
        return LocalPriceCompareAdapter()
    if resolved.shopping_compare_provider == "haodanku":
        from assistant_agent.providers.haodanku_product_search import (
            HaodankuConfig,
            HaodankuPriceCompareAdapter,
        )

        return HaodankuPriceCompareAdapter(
            HaodankuConfig(
                api_key=resolved.haodanku_api_key,
                base_url=resolved.haodanku_base_url,
                timeout_seconds=resolved.haodanku_timeout_seconds,
                enabled_platforms=resolved.haodanku_enabled_platforms,
                taobao_pid=resolved.haodanku_taobao_pid,
                taobao_authorized_name=resolved.haodanku_taobao_authorized_name,
                jd_sub_union_id=resolved.haodanku_jd_sub_union_id,
                pdd_channel=resolved.haodanku_pdd_channel,
            )
        )
    if resolved.shopping_compare_provider == "http":
        return HttpPriceCompareAdapter(
            base_url=resolved.shopping_compare_base_url,
            api_key=resolved.shopping_compare_api_key,
            timeout_seconds=resolved.shopping_compare_timeout_seconds,
        )
    return MockPriceCompareAdapter()


def _mock_products() -> list[ProductResult]:
    return [
        ProductResult(
            product_id="p1",
            title="白色低帮运动鞋 A",
            brand="Mock",
            category="shoes",
            price=299.0,
            platform="mock-shop-a",
            shop="mock-shop-a",
            url="mock://shop-a/p1",
            product_url="mock://shop-a/p1",
            image_url="mock://images/p1.png",
            similarity=0.92,
            similarity_score=0.92,
            text_match_score=0.88,
            rating=4.7,
            color="white",
            material="synthetic leather",
            style_tags=["low-top", "minimal"],
            reason="颜色、鞋型和材质相似度最高",
            ranking_reason=RankingReason(
                score=0.9,
                factors={"visual_similarity": 0.92, "text_match": 0.88, "price_match": 0.9},
                explanation="颜色、鞋型和材质相似度最高，价格在常规预算范围内。",
            ),
            source="mock",
        ),
        ProductResult(
            product_id="p2",
            title="简约白色板鞋 B",
            brand="Mock",
            category="shoes",
            price=259.0,
            platform="mock-shop-b",
            shop="mock-shop-b",
            url="mock://shop-b/p2",
            product_url="mock://shop-b/p2",
            image_url="mock://images/p2.png",
            similarity=0.86,
            similarity_score=0.86,
            text_match_score=0.84,
            rating=4.5,
            color="white",
            material="canvas",
            style_tags=["minimal", "budget"],
            reason="价格更低，外观接近",
            ranking_reason=RankingReason(
                score=0.86,
                factors={"visual_similarity": 0.86, "text_match": 0.84, "price_match": 0.95},
                explanation="价格更低，外观接近，适合作为高性价比候选。",
            ),
            source="mock",
        ),
        ProductResult(
            product_id="p3",
            title="日系白色休闲鞋 C",
            brand="Mock",
            category="shoes",
            price=339.0,
            platform="mock-shop-c",
            shop="mock-shop-c",
            url="mock://shop-c/p3",
            product_url="mock://shop-c/p3",
            image_url="mock://images/p3.png",
            similarity=0.81,
            similarity_score=0.81,
            text_match_score=0.8,
            rating=4.8,
            color="white",
            material="canvas",
            style_tags=["japanese", "casual"],
            reason="风格接近，评分较高",
            ranking_reason=RankingReason(
                score=0.83,
                factors={"visual_similarity": 0.81, "text_match": 0.8, "rating": 0.96},
                explanation="风格接近且评分较高，但价格略高。",
            ),
            source="mock",
        ),
    ]


def _load_local_products(path: Path) -> list[ProductResult]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_items: list[dict[str, Any]]
    if isinstance(payload, list):
        raw_items = payload
    elif isinstance(payload, dict) and isinstance(payload.get("items"), list):
        raw_items = payload["items"]
    else:
        raise ValueError("expected a list or an object with items")
    return [ProductResult.model_validate(item) for item in raw_items]


def _filter_products(items: list[ProductResult], request: ProductSearchRequest) -> list[ProductResult]:
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

    tokens = _query_tokens(request)
    if tokens:
        matched = [
            item
            for item in filtered
            if any(token in f"{item.title} {item.reason or ''} {item.platform}".lower() for token in tokens)
        ]
        if len(matched) >= 2 or len(filtered) == 1:
            filtered = matched

    return filtered[: request.top_k]


def _query_text(request: ProductSearchRequest) -> str:
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


def _query_tokens(request: ProductSearchRequest) -> list[str]:
    text = _query_text(request).lower()
    return [token for token in text.replace("/", " ").split() if len(token) >= 2]


def _filters_used(request: ProductSearchRequest) -> dict[str, object]:
    filters: dict[str, object] = {}
    for key in ("brand", "category", "budget_min", "budget_max", "budget", "platforms", "top_k"):
        value = getattr(request, key)
        if value not in (None, [], ""):
            filters[key] = value
    return filters


def _failed_search_result(
    provider: str,
    code: str,
    message: str,
    recoverable: bool,
) -> ProductSearchResult:
    error = build_provider_error(
        code,
        message,
        recoverable=recoverable,
        provider=provider,
        capability=SHOPPING_SEARCH_CAPABILITY,
    )
    return ProductSearchResult(
        provider=provider,
        errors=[ProductProviderError(code=error.code, message=error.message, recoverable=error.recoverable)],
        total=0,
    )


def _compare_products(
    items: list[ProductResult],
    request: PriceCompareRequest,
    *,
    provider: str,
    output_ref: str | None = None,
    latency_ms: int | None = None,
) -> PriceCompareResult:
    """Rank product candidates into a structured price comparison result.

    Shared by the mock/local adapters and the Haodanku adapter so every
    provider produces an identical, explainable ranking.
    """

    if not items:
        return _failed_price_result(
            provider=provider,
            query=request.query,
            code="price_no_products",
            message="缺少商品候选列表，无法比价",
            recoverable=True,
        )

    offers = [_offer_from_product(item) for item in items if item.price is not None]
    offers = _filter_offers(offers, request)
    if not offers:
        return _failed_price_result(
            provider=provider,
            query=request.query,
            code="price_no_offers",
            message="没有符合预算或平台条件的报价。",
            recoverable=True,
        )

    offers = _sort_offers(offers, request.sort_by)[: request.top_k]
    best_offer = offers[0]
    items_by_id = {item.product_id: item for item in items}
    sorted_items = [items_by_id[offer.product_id] for offer in offers if offer.product_id in items_by_id]
    ranking_reason = _ranking_reason(best_offer, request.sort_by)
    return PriceCompareResult(
        query=request.query,
        items=sorted_items,
        best_value_product_id=best_offer.product_id,
        summary=f"{best_offer.title} 当前综合最优，总价 {best_offer.total_price:.2f} {best_offer.currency}。",
        offers=offers,
        best_offer=best_offer,
        ranking_reason=ranking_reason,
        provider=provider,
        latency_ms=latency_ms,
        output_ref=output_ref,
    )


def _offer_from_product(product: ProductResult) -> PriceOffer:
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


def _filter_offers(offers: list[PriceOffer], request: PriceCompareRequest) -> list[PriceOffer]:
    filtered = offers
    if request.platforms:
        platforms = set(request.platforms)
        filtered = [offer for offer in filtered if offer.platform in platforms]
    if request.budget_min is not None:
        filtered = [offer for offer in filtered if offer.total_price >= request.budget_min]
    if request.budget_max is not None:
        filtered = [offer for offer in filtered if offer.total_price <= request.budget_max]
    return filtered


def _sort_offers(offers: list[PriceOffer], sort_by: str) -> list[PriceOffer]:
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


def _ranking_reason(best_offer: PriceOffer, sort_by: str) -> RankingReason:
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


def _failed_price_result(
    provider: str,
    query: str,
    code: str,
    message: str,
    recoverable: bool,
) -> PriceCompareResult:
    error = build_provider_error(
        code,
        message,
        recoverable=recoverable,
        provider=provider,
        capability=SHOPPING_SEARCH_CAPABILITY,
    )
    return PriceCompareResult(
        query=query or SHOPPING_SEARCH_CAPABILITY,
        summary=error.message,
        provider=provider,
        errors=[ProductProviderError(code=error.code, message=error.message, recoverable=error.recoverable)],
    )
