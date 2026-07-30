"""Haodanku (好单库) product search and price comparison provider adapter.

Haodanku's v3 ``supersearch`` endpoint returns coupon-aware Taobao items
(券后价 / 优惠券 / 佣金 / 销量 / 主图 / 店铺 / 商品链接), which makes it a
natural real provider for the assistant's "search + price compare" flow.

The HTTP transport intentionally uses ``urllib.request`` to match the existing
provider adapters (see ``providers/ark_image_generation.py``) and avoid new
dependencies. Pure helpers (``build_haodanku_search_url`` / ``map_haodanku_items``)
are split out so they can be unit tested without network IO.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from assistant_agent.tools.plugins.builtin.shopping.models import (
    PriceCompareRequest,
    PriceCompareResult,
    PriceOffer,
    ProductProviderError,
    ProductResult,
    ProductSearchRequest,
    ProductSearchResult,
)
from assistant_agent.tools.plugins.builtin.shopping.product_matching import (
    compare_products,
    failed_price_result,
    failed_search_result,
    filter_products,
    filters_used,
    normalize_provider_limit,
    query_text,
)


DEFAULT_HAODANKU_BASE_URL = "https://v3.api.haodanku.com"
DEFAULT_HAODANKU_TIMEOUT_SECONDS = 10.0
DEFAULT_HAODANKU_BACK = 10
HAODANKU_BACK_VALUES = (1, 2, 5, 10, 20, 50, 100)
DEFAULT_HAODANKU_SORT = "0"
HAODANKU_LINKED_MIN_BACK = 20
HAODANKU_LINKED_MEDIUM_BACK = 50
HAODANKU_LINKED_BACK_MULTIPLIER = 3
HAODANKU_JD_BACK_VALUES = (1, 2, 5, 10, 20, 30, 50)
HAODANKU_PDD_BACK_VALUES = (10, 20, 30, 40, 50, 60, 70, 80, 90, 100)
COUPON_LINK_FIELDS = (
    "couponurl",
    "coupon_url",
    "coupon_click_url",
    "click_url",
    "clickURL",
    "shortURL",
    "short_url",
    "mobile_url",
    "mobile_short_url",
    "schema_url",
    "share_link",
    "dy_deeplink",
    "dy_zlink",
    "kwaiUrl",
    "linkUrl",
    "deeplink",
    "deeplinkUrl",
    "deep_link",
    "trans_url",
    "referral_link",
    "tb_scheme_url",
    "ele_scheme_url",
    "alipay_mini_url",
)
LANDING_LINK_FIELDS = (
    "itemlink",
    "item_url",
    "itemurl",
    "url",
    "longUrl",
    "h5_url",
    "h5_short_link",
)


@dataclass(frozen=True)
class HaodankuConfig:
    """Configuration for the optional Haodanku product provider."""

    api_key: str | None
    base_url: str = DEFAULT_HAODANKU_BASE_URL
    timeout_seconds: float = DEFAULT_HAODANKU_TIMEOUT_SECONDS
    sort: str = DEFAULT_HAODANKU_SORT
    enabled_platforms: tuple[str, ...] = ("taobao",)
    taobao_pid: str | None = None
    taobao_authorized_name: str | None = None
    jd_sub_union_id: str | None = None
    pdd_channel: str | None = None


@dataclass(frozen=True)
class ProductLinkMetadata:
    """Provider link fields after conservative local normalization."""

    product_url: str | None
    raw_url: str | None
    landing_url: str | None
    coupon_url: str | None
    click_url: str | None
    url_status: str


class HaodankuProductSearchAdapter:
    """Keyword product search backed by the Haodanku v3 ``supersearch`` API."""

    provider = "haodanku"

    def __init__(self, config: HaodankuConfig) -> None:
        self.config = config

    def search(self, request: ProductSearchRequest) -> ProductSearchResult:
        if not self.config.api_key:
            return failed_search_result(
                provider=self.provider,
                code="provider_unconfigured",
                message="haodanku product search provider is missing HAODANKU_API_KEY.",
                recoverable=True,
            )

        keyword = query_text(request)
        if not keyword:
            return failed_search_result(
                provider=self.provider,
                code="product_query_empty",
                message="缺少商品描述，无法搜索",
                recoverable=True,
            )

        requested_platforms = _requested_platforms(
            request.platforms,
            enabled_platforms=self.config.enabled_platforms,
        )
        if not requested_platforms:
            return _platform_disabled_search_result(keyword, request.platforms)
        results: dict[str, tuple[list[ProductResult], ProductProviderError | None, dict[str, Any]]] = {}
        with ThreadPoolExecutor(max_workers=len(requested_platforms)) as executor:
            futures = {
                executor.submit(self._search_platform, platform, keyword, request.top_k): platform
                for platform in requested_platforms
            }
            for future in as_completed(futures):
                platform = futures[future]
                try:
                    results[platform] = future.result()
                except Exception as exc:  # pragma: no cover - defensive worker boundary.
                    results[platform] = (
                        [],
                        ProductProviderError(
                            code="provider_execution_failed",
                            message=str(exc),
                            recoverable=True,
                        ),
                        {},
                    )

        items: list[ProductResult] = []
        succeeded: list[str] = []
        failed: list[str] = []
        platform_errors: dict[str, list[ProductProviderError]] = {}
        used_filters = filters_used(request)
        for platform in requested_platforms:
            platform_items, error, platform_metadata = results[platform]
            if error is not None:
                failed.append(platform)
                platform_errors[platform] = [error]
            else:
                succeeded.append(platform)
                items.extend(platform_items[: request.top_k])
            if platform_metadata:
                used_key = "platform_metrics"
                used_filters.setdefault(used_key, {})
                used_filters[used_key][platform] = platform_metadata

        if items:
            filter_request = request.model_copy(
                update={"platforms": requested_platforms, "top_k": len(items)}
            )
            items = filter_products(items, filter_request)
        used_filters["per_platform_top_k"] = request.top_k
        if requested_platforms == ["taobao"]:
            used_filters.update(used_filters.get("platform_metrics", {}).get("taobao", {}))
        errors = [error for platform in failed for error in platform_errors[platform]]
        return ProductSearchResult(
            items=items,
            provider=self.provider,
            query_used=keyword,
            filters_used=used_filters,
            total=len(items),
            errors=errors,
            output_ref=f"haodanku://search/{urllib.parse.quote(keyword)}",
            requested_platforms=requested_platforms,
            succeeded_platforms=succeeded,
            failed_platforms=failed,
            platform_errors=platform_errors,
        )

    def _search_platform(
        self,
        platform: str,
        keyword: str,
        top_k: int,
    ) -> tuple[list[ProductResult], ProductProviderError | None, dict[str, Any]]:
        provider_limit = _linked_search_back_request(top_k) if platform == "taobao" else top_k
        if platform == "taobao":
            provider_limit = normalize_provider_limit(
                provider_limit,
                default=DEFAULT_HAODANKU_BACK,
                allowed_values=HAODANKU_BACK_VALUES,
            ).provider_limit
        elif platform in {"jd", "pdd"}:
            provider_limit = _normalize_platform_back(platform, top_k)
        url = build_haodanku_platform_search_url(
            base_url=self.config.base_url,
            api_key=self.config.api_key or "",
            platform=platform,
            keyword=keyword,
            limit=provider_limit,
        )
        http_request = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(http_request, timeout=self.config.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except TimeoutError as exc:
            return [], ProductProviderError(code="provider_timeout", message=str(exc), recoverable=True), {}
        except urllib.error.HTTPError as exc:
            return [], ProductProviderError(code=_http_status_to_error_code(exc.code), message=f"HTTP {exc.code}"), {}
        except urllib.error.URLError as exc:
            return [], ProductProviderError(code="provider_network_error", message=str(exc.reason), recoverable=True), {}
        except json.JSONDecodeError:
            return [], ProductProviderError(code="provider_bad_response", message="haodanku response JSON decode failed"), {}

        error_message = _haodanku_error_message(payload)
        if error_message is not None:
            return [], ProductProviderError(
                code="provider_bad_response",
                message=_normalized_haodanku_error_message(error_message),
            ), {}
        items = map_haodanku_platform_items(platform, payload)
        metadata: dict[str, Any] = {"provider_back": provider_limit}
        if platform == "taobao":
            candidate_count = len(items)
            items = [item for item in items if _has_product_url(item)]
            metadata.update(
                {
                    "linked_only": True,
                    "linked_items_found": len(items),
                    "linked_items_returned": min(len(items), top_k),
                    "unlinked_items_dropped": candidate_count - len(items),
                }
            )
        return items[:top_k], None, metadata

    def compare(self, request: PriceCompareRequest) -> PriceCompareResult:
        return HaodankuPriceCompareAdapter(self.config, search_adapter=self).compare(request)


class HaodankuPriceCompareAdapter:
    """Price comparison over Haodanku candidates.

    Reuses the shared offer-building and ranking helpers; when no candidate
    items are supplied it first runs a Haodanku keyword search so a single
    request can perform "search → compare".
    """

    provider = "haodanku"

    def __init__(
        self,
        config: HaodankuConfig,
        search_adapter: HaodankuProductSearchAdapter | None = None,
    ) -> None:
        self.config = config
        self._search_adapter = search_adapter or HaodankuProductSearchAdapter(config)

    def compare(self, request: PriceCompareRequest) -> PriceCompareResult:
        normalized_platforms = _requested_platforms(
            request.platforms,
            enabled_platforms=self.config.enabled_platforms,
        )
        if request.platforms and not normalized_platforms:
            return failed_price_result(
                provider=self.provider,
                query=request.query,
                code="provider_platform_disabled",
                message="请求的平台未在 HAODANKU_ENABLED_PLATFORMS 中启用。",
                recoverable=True,
            )
        normalized_request = request.model_copy(update={"platforms": normalized_platforms})
        items = list(request.items)
        if not items:
            if not request.query:
                return failed_price_result(
                    provider=self.provider,
                    query=request.query,
                    code="price_no_products",
                    message="缺少商品候选列表，无法比价",
                    recoverable=True,
                )
            search_result = self._search_adapter.search(
                ProductSearchRequest(
                    query=request.query,
                    platforms=normalized_platforms,
                    top_k=request.top_k,
                )
            )
            if not search_result.items:
                if not search_result.errors:
                    return failed_price_result(
                        provider=self.provider,
                        query=request.query,
                        code="price_no_products",
                        message="没有搜索到可比较的商品。",
                        recoverable=True,
                    )
                error = search_result.errors[0]
                return failed_price_result(
                    provider=self.provider,
                    query=request.query,
                    code=error.code,
                    message=error.message,
                    recoverable=error.recoverable,
                )
            items = search_result.items

        result = compare_products(items, normalized_request, provider=self.provider)
        if result.errors:
            return result
        converted = [self._convert_offer(offer, items) for offer in result.offers]
        best_id = result.best_offer.offer_id if result.best_offer is not None else None
        best = next((offer for offer in converted if offer.offer_id == best_id), None)
        return result.model_copy(update={"offers": converted, "best_offer": best})

    def _convert_offer(self, offer: PriceOffer, items: list[ProductResult]) -> PriceOffer:
        item = next((candidate for candidate in items if candidate.product_id == offer.product_id), None)
        if item is None or not self.config.api_key:
            return _safe_fallback_offer(offer)
        platform = "taobao" if offer.platform == "tmall" else offer.platform
        params: dict[str, Any] = {"apikey": self.config.api_key}
        if platform == "taobao":
            if not self.config.taobao_pid or not self.config.taobao_authorized_name:
                return _safe_fallback_offer(offer)
            endpoint = "ratesurl"
            params.update(
                {
                    "itemid": item.provider_item_id or item.product_id,
                    "pid": self.config.taobao_pid,
                    "tb_name": self.config.taobao_authorized_name,
                    "get_taoword": 1,
                    "title": item.title,
                }
            )
        elif platform == "jd":
            endpoint = "unify_jditems_link"
            params["material_id"] = item.product_url or item.provider_item_id or item.product_id
            if self.config.jd_sub_union_id:
                params["subUnionId"] = self.config.jd_sub_union_id
        elif platform == "pdd":
            endpoint = "unify_pdditems_link"
            params["itemid"] = item.provider_item_id or item.product_id
            if self.config.pdd_channel:
                params["channel"] = self.config.pdd_channel
        else:
            return _safe_fallback_offer(offer)

        request = urllib.request.Request(
            f"{_normalize_haodanku_base_url(self.config.base_url)}/{endpoint}",
            data=urllib.parse.urlencode(params).encode("utf-8"),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (TimeoutError, urllib.error.URLError, json.JSONDecodeError):
            return _safe_fallback_offer(offer)
        if _haodanku_error_message(payload) is not None:
            return _safe_fallback_offer(offer)
        converted = _conversion_url(platform, payload)
        if not _valid_platform_url(converted, platform):
            return _safe_fallback_offer(offer)
        return offer.model_copy(update={"product_url": converted, "url_status": "verified"})


def build_haodanku_search_url(
    *,
    base_url: str,
    api_key: str,
    keyword: str,
    back: int = DEFAULT_HAODANKU_BACK,
    min_id: int = 1,
    sort: str = DEFAULT_HAODANKU_SORT,
) -> str:
    """Build the Haodanku v3 Taobao keyword search URL."""

    normalized = _normalize_haodanku_base_url(base_url)
    query = urllib.parse.urlencode(
        {
            "apikey": api_key,
            "keyword": keyword,
            "back": back,
            "min_id": min_id,
        }
    )
    return f"{normalized}/supersearch?{query}"


def build_haodanku_platform_search_url(
    *,
    base_url: str,
    api_key: str,
    platform: str,
    keyword: str,
    limit: int,
) -> str:
    endpoint = {
        "taobao": "supersearch",
        "jd": "unify_jdgoods_search",
        "pdd": "unify_pdd_goods_search",
    }[platform]
    provider_limit = _normalize_platform_back(platform, limit)
    query = urllib.parse.urlencode(
        {"apikey": api_key, "keyword": keyword, "back": provider_limit, "min_id": 1}
    )
    return f"{_normalize_haodanku_base_url(base_url)}/{endpoint}?{query}"


def _normalize_platform_back(platform: str, requested: int) -> int:
    """Map provider-neutral result counts onto platform-supported page sizes."""

    allowed_values = {
        "jd": HAODANKU_JD_BACK_VALUES,
        "pdd": HAODANKU_PDD_BACK_VALUES,
    }.get(platform)
    if allowed_values is None:
        return requested
    return normalize_provider_limit(
        requested,
        default=allowed_values[0],
        allowed_values=allowed_values,
    ).provider_limit


def _requested_platforms(
    platforms: list[str],
    *,
    enabled_platforms: tuple[str, ...],
) -> list[str]:
    requested = platforms or list(enabled_platforms)
    normalized: list[str] = []
    for platform in requested:
        value = {
            "淘宝": "taobao",
            "天猫": "taobao",
            "tmall": "taobao",
            "京东": "jd",
            "拼多多": "pdd",
        }.get(platform.strip().lower(), platform.strip().lower())
        if value in enabled_platforms and value not in normalized:
            normalized.append(value)
    return normalized


def _normalized_haodanku_error_message(message: str) -> str:
    if message.strip() == "数据已获取完毕或获取数据失败!":
        return "好单库未返回可用商品数据。"
    return message


def _platform_disabled_search_result(
    keyword: str,
    requested_platforms: list[str],
) -> ProductSearchResult:
    error = ProductProviderError(
        code="provider_platform_disabled",
        message="请求的平台未在 HAODANKU_ENABLED_PLATFORMS 中启用。",
        recoverable=True,
    )
    return ProductSearchResult(
        provider="haodanku",
        query_used=keyword,
        filters_used={"platforms": list(requested_platforms)},
        total=0,
        errors=[error],
        requested_platforms=[],
        succeeded_platforms=[],
        failed_platforms=[],
        platform_errors={},
    )


def _linked_search_back_request(top_k: int | None) -> int:
    requested = top_k or DEFAULT_HAODANKU_BACK
    if requested <= 5:
        return HAODANKU_LINKED_MIN_BACK
    if requested <= 10:
        return HAODANKU_LINKED_MEDIUM_BACK
    return max(requested * HAODANKU_LINKED_BACK_MULTIPLIER, HAODANKU_LINKED_MEDIUM_BACK)


def map_haodanku_items(payload: Any) -> list[ProductResult]:
    """Map a Haodanku keyword response into stable ``ProductResult`` items."""

    raw_items = _extract_raw_items(payload)
    products: list[ProductResult] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        product = _map_single_item(raw)
        if product is not None:
            products.append(product)
    return products


def map_haodanku_platform_items(platform: str, payload: Any) -> list[ProductResult]:
    """Map one platform payload into the provider-neutral contract."""

    normalized = "taobao" if platform in {"taobao", "tmall"} else platform
    products: list[ProductResult] = []
    for raw in _extract_raw_items(payload):
        if not isinstance(raw, dict):
            continue
        title = _clean_str(raw.get("itemtitle") or raw.get("goodsname"))
        item_id = _clean_str(raw.get("goods_sign") or raw.get("itemid") or raw.get("item_id"))
        original_price = _to_float(raw.get("itemprice"))
        effective_price = _to_float(raw.get("itemendprice")) or original_price
        if not title or not item_id or effective_price is None:
            continue
        link = _product_link_metadata(raw, provider_item_id=item_id) if normalized == "taobao" else ProductLinkMetadata(
            product_url=_first_provider_link(raw, COUPON_LINK_FIELDS + LANDING_LINK_FIELDS),
            raw_url=_first_provider_link(raw, COUPON_LINK_FIELDS + LANDING_LINK_FIELDS),
            landing_url=_first_provider_link(raw, LANDING_LINK_FIELDS),
            coupon_url=_first_provider_link(raw, COUPON_LINK_FIELDS),
            click_url=_first_provider_link(raw, COUPON_LINK_FIELDS),
            url_status="unverified",
        )
        if link.product_url is None:
            direct_url = _platform_direct_url(normalized, item_id)
            link = ProductLinkMetadata(
                product_url=direct_url,
                raw_url=None,
                landing_url=direct_url,
                coupon_url=None,
                click_url=None,
                url_status="unverified" if direct_url else "missing",
            )
        products.append(
            ProductResult(
                product_id=item_id if normalized == "taobao" else f"{normalized}:{item_id}",
                provider_item_id=item_id,
                title=title,
                brand=_clean_str(raw.get("brand_name")),
                price=effective_price,
                original_price=original_price,
                coupon_amount=_to_float(raw.get("couponmoney")),
                effective_price=effective_price,
                unconditional_price=effective_price,
                platform=normalized,
                shop=_clean_str(raw.get("shopname")),
                url=link.product_url,
                product_url=link.product_url,
                raw_url=link.raw_url,
                landing_url=link.landing_url,
                coupon_url=link.coupon_url,
                click_url=link.click_url,
                url_status=link.url_status,
                image_url=_clean_str(raw.get("itempic")),
                sales=_to_int(raw.get("itemsale")),
                source="haodanku",
            )
        )
    return products


def _platform_direct_url(platform: str, item_id: str) -> str | None:
    encoded = urllib.parse.quote(item_id, safe="")
    if platform == "jd" and item_id.isdigit():
        return f"https://item.jd.com/{encoded}.html"
    if platform == "pdd":
        return f"https://mobile.yangkeduo.com/goods.html?goods_id={encoded}"
    return None


def _conversion_url(platform: str, payload: Any) -> str | None:
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        return None
    fields = {
        "taobao": ("coupon_click_url", "item_url"),
        "jd": ("clickURL", "shortURL"),
        "pdd": ("url", "short_url", "mobile_url", "mobile_short_url"),
    }[platform]
    return _first_provider_link(data, fields)


def _valid_platform_url(value: str | None, platform: str) -> bool:
    if not value:
        return False
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.path:
        return False
    host = parsed.hostname or ""
    allowed = {
        "taobao": ("taobao.com", "tmall.com"),
        "jd": ("jd.com",),
        "pdd": ("yangkeduo.com", "pinduoduo.com"),
    }[platform]
    return any(host == domain or host.endswith(f".{domain}") for domain in allowed)


def _safe_fallback_offer(offer: PriceOffer) -> PriceOffer:
    platform = "taobao" if offer.platform == "tmall" else offer.platform
    if _valid_platform_url(offer.product_url, platform):
        return offer.model_copy(update={"url_status": "unverified"})
    return offer.model_copy(update={"product_url": None, "url_status": "missing"})


def _map_single_item(raw: dict[str, Any]) -> ProductResult | None:
    title = _clean_str(raw.get("itemtitle") or raw.get("itemshorttitle"))
    if not title:
        return None
    provider_item_id = _clean_str(raw.get("itemid")) or _clean_str(raw.get("item_id"))
    if not provider_item_id:
        return None

    end_price = _to_float(raw.get("itemendprice"))
    price = end_price if end_price is not None else _to_float(raw.get("itemprice"))
    if price is None:
        return None

    coupon = _to_float(raw.get("couponmoney"))
    commission_rate = (
        _clean_str(raw.get("commission_rate"))
        or _clean_str(raw.get("tkrates"))
        or _clean_str(raw.get("commission"))
    )
    image = _clean_str(raw.get("itempic"))
    shop = _clean_str(raw.get("shopname")) or _clean_str(raw.get("itemshorttitle"))
    sales = _to_int(raw.get("itemsale") or raw.get("monthsales") or raw.get("itemsale_str"))
    platform = _platform_from_shoptype(raw.get("shoptype"))
    link = _product_link_metadata(raw, provider_item_id=provider_item_id)

    style_tags: list[str] = []
    reason_parts: list[str] = []
    if coupon and coupon > 0:
        style_tags.append("coupon")
        reason_parts.append(f"含 {coupon:.0f} 元优惠券，券后价 {price:.2f}")
    if commission_rate:
        reason_parts.append(f"佣金比例 {commission_rate}")
    reason = "；".join(reason_parts) if reason_parts else None

    return ProductResult(
        product_id=provider_item_id,
        provider_item_id=provider_item_id,
        title=title,
        category=_clean_str(raw.get("itemtitle_cat")) or None,
        price=price,
        original_price=_to_float(raw.get("itemprice")),
        coupon_amount=coupon,
        effective_price=price,
        unconditional_price=price,
        platform=platform,
        shop=shop,
        url=link.product_url,
        product_url=link.product_url,
        raw_url=link.raw_url,
        landing_url=link.landing_url,
        coupon_url=link.coupon_url,
        click_url=link.click_url,
        url_status=link.url_status,
        availability="unknown",
        image_url=image,
        sales=sales,
        style_tags=style_tags,
        reason=reason,
        source="haodanku",
    )


def _platform_from_shoptype(value: Any) -> str:
    # Tmall stays in the Taobao display/comparison group.
    return "taobao"


def _product_link_metadata(raw: dict[str, Any], *, provider_item_id: str) -> ProductLinkMetadata:
    coupon_url = _first_provider_link(raw, COUPON_LINK_FIELDS)
    landing_url = _first_provider_link(raw, LANDING_LINK_FIELDS)
    if not _valid_platform_url(coupon_url, "taobao"):
        coupon_url = None
    if not _valid_platform_url(landing_url, "taobao"):
        landing_url = None
    direct = coupon_url or landing_url
    if direct:
        return ProductLinkMetadata(
            product_url=direct,
            raw_url=direct,
            landing_url=landing_url,
            coupon_url=coupon_url,
            click_url=coupon_url,
            url_status="unverified",
        )
    if provider_item_id.isdigit():
        encoded_id = urllib.parse.quote(provider_item_id, safe="")
        return ProductLinkMetadata(
            product_url=f"https://item.taobao.com/item.htm?id={encoded_id}",
            raw_url=None,
            landing_url=None,
            coupon_url=None,
            click_url=None,
            url_status="unverified",
        )
    return ProductLinkMetadata(
        product_url=None,
        raw_url=None,
        landing_url=None,
        coupon_url=None,
        click_url=None,
        url_status="invalid_id",
    )


def _has_product_url(item: ProductResult) -> bool:
    return bool(item.product_url or item.url)


def _first_provider_link(raw: dict[str, Any], fields: tuple[str, ...]) -> str | None:
    for field in fields:
        value = _normalize_provider_link(raw.get(field))
        if value:
            return value
    return None


def _normalize_provider_link(value: Any) -> str | None:
    text = _clean_str(value)
    if not text:
        return None
    if text.startswith("//"):
        text = f"https:{text}"
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return text
    return None


def _normalize_haodanku_base_url(base_url: str | None) -> str:
    normalized = (base_url or DEFAULT_HAODANKU_BASE_URL).rstrip("/")
    # Older local configs used v2 with the removed /keyword path. Product docs
    # now specify v3 /supersearch, so keep legacy env files from producing 404.
    return normalized.replace("v2.api.haodanku.com", "v3.api.haodanku.com")


def _extract_raw_items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("list"), list):
            return data["list"]
    return []


def _haodanku_error_message(payload: Any) -> str | None:
    """Return an error message if the Haodanku envelope reports a failure."""

    if not isinstance(payload, dict):
        return None
    code = payload.get("code")
    # Haodanku returns code==1 on success; anything else carries a message.
    if code is not None and str(code) not in {"1", "200"}:
        message = _clean_str(payload.get("msg") or payload.get("message")) or f"haodanku error code {code}"
        return message
    return None


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _http_status_to_error_code(status: int) -> str:
    if status == 401:
        return "provider_auth_failed"
    if status == 403:
        return "provider_permission_denied"
    if status == 429:
        return "provider_rate_limited"
    if status >= 500:
        return "provider_bad_gateway"
    if status >= 400:
        return "provider_bad_response"
    return "provider_execution_failed"
