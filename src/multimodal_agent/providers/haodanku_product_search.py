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
from dataclasses import dataclass
from typing import Any

from multimodal_agent.schemas.products import (
    PriceCompareRequest,
    PriceCompareResult,
    ProductResult,
    ProductSearchRequest,
    ProductSearchResult,
)
from multimodal_agent.utils.product_matching import (
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

        limit = normalize_provider_limit(
            request.top_k,
            default=DEFAULT_HAODANKU_BACK,
            allowed_values=HAODANKU_BACK_VALUES,
        )
        url = build_haodanku_search_url(
            base_url=self.config.base_url,
            api_key=self.config.api_key,
            keyword=keyword,
            back=limit.provider_limit,
            sort=self.config.sort,
        )
        http_request = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(http_request, timeout=self.config.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except TimeoutError as exc:
            return failed_search_result(
                provider=self.provider,
                code="provider_timeout",
                message=str(exc),
                recoverable=True,
            )
        except urllib.error.HTTPError as exc:
            return failed_search_result(
                provider=self.provider,
                code=_http_status_to_error_code(exc.code),
                message=f"HTTP {exc.code}",
                recoverable=False,
            )
        except urllib.error.URLError as exc:
            return failed_search_result(
                provider=self.provider,
                code="provider_network_error",
                message=str(exc.reason),
                recoverable=True,
            )
        except json.JSONDecodeError:
            return failed_search_result(
                provider=self.provider,
                code="provider_bad_response",
                message="haodanku response JSON decode failed",
                recoverable=False,
            )

        error_message = _haodanku_error_message(payload)
        if error_message is not None:
            return failed_search_result(
                provider=self.provider,
                code="provider_bad_response",
                message=error_message,
                recoverable=False,
            )

        items = map_haodanku_items(payload)
        filtered = filter_products(items, request)
        used_filters = filters_used(request)
        used_filters["provider_back"] = limit.provider_limit
        if limit.normalized:
            used_filters["requested_top_k"] = limit.requested
            used_filters["limit_normalized"] = True
        if limit.capped:
            used_filters["limit_capped"] = True
        return ProductSearchResult(
            items=filtered,
            provider=self.provider,
            query_used=keyword,
            filters_used=used_filters,
            total=len(filtered),
            output_ref=f"haodanku://search/{urllib.parse.quote(keyword)}",
        )

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
                ProductSearchRequest(query=request.query, top_k=request.top_k)
            )
            if search_result.errors:
                error = search_result.errors[0]
                return failed_price_result(
                    provider=self.provider,
                    query=request.query,
                    code=error.code,
                    message=error.message,
                    recoverable=error.recoverable,
                )
            items = search_result.items

        return compare_products(items, request, provider=self.provider)


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
    text = _clean_str(value)
    if text in {"1", "B"}:
        return "tmall"
    return "taobao"


def _product_link_metadata(raw: dict[str, Any], *, provider_item_id: str) -> ProductLinkMetadata:
    coupon_url = _first_provider_link(raw, COUPON_LINK_FIELDS)
    landing_url = _first_provider_link(raw, LANDING_LINK_FIELDS)
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
