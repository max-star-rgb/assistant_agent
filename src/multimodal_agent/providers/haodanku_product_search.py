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
    PriceCompareResult,
    ProductResult,
    ProductSearchResult,
)


DEFAULT_HAODANKU_BASE_URL = "https://v3.api.haodanku.com"
DEFAULT_HAODANKU_TIMEOUT_SECONDS = 10.0
DEFAULT_HAODANKU_BACK = 10
DEFAULT_HAODANKU_SORT = "0"


@dataclass(frozen=True)
class HaodankuConfig:
    """Configuration for the optional Haodanku product provider."""

    api_key: str | None
    base_url: str = DEFAULT_HAODANKU_BASE_URL
    timeout_seconds: float = DEFAULT_HAODANKU_TIMEOUT_SECONDS
    sort: str = DEFAULT_HAODANKU_SORT


class HaodankuProductSearchAdapter:
    """Keyword product search backed by the Haodanku v3 ``supersearch`` API."""

    provider = "haodanku"

    def __init__(self, config: HaodankuConfig) -> None:
        self.config = config

    def search(self, request: "ProductSearchRequest") -> ProductSearchResult:
        if not self.config.api_key:
            return _failed_search_result(
                provider=self.provider,
                code="provider_unconfigured",
                message="haodanku product search provider is missing HAODANKU_API_KEY.",
                recoverable=True,
            )

        keyword = _query_text(request)
        if not keyword:
            return _failed_search_result(
                provider=self.provider,
                code="product_query_empty",
                message="缺少商品描述，无法搜索",
                recoverable=True,
            )

        url = build_haodanku_search_url(
            base_url=self.config.base_url,
            api_key=self.config.api_key,
            keyword=keyword,
            back=request.top_k or DEFAULT_HAODANKU_BACK,
            sort=self.config.sort,
        )
        http_request = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(http_request, timeout=self.config.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except TimeoutError as exc:
            return _failed_search_result(
                provider=self.provider,
                code="provider_timeout",
                message=str(exc),
                recoverable=True,
            )
        except urllib.error.HTTPError as exc:
            return _failed_search_result(
                provider=self.provider,
                code=_http_status_to_error_code(exc.code),
                message=f"HTTP {exc.code}",
                recoverable=False,
            )
        except urllib.error.URLError as exc:
            return _failed_search_result(
                provider=self.provider,
                code="provider_network_error",
                message=str(exc.reason),
                recoverable=True,
            )
        except json.JSONDecodeError:
            return _failed_search_result(
                provider=self.provider,
                code="provider_bad_response",
                message="haodanku response JSON decode failed",
                recoverable=False,
            )

        error_message = _haodanku_error_message(payload)
        if error_message is not None:
            return _failed_search_result(
                provider=self.provider,
                code="provider_bad_response",
                message=error_message,
                recoverable=False,
            )

        items = map_haodanku_items(payload)
        filtered = _filter_products(items, request)
        return ProductSearchResult(
            items=filtered,
            provider=self.provider,
            query_used=keyword,
            filters_used=_filters_used(request),
            total=len(filtered),
            output_ref=f"haodanku://search/{urllib.parse.quote(keyword)}",
        )

    def compare(self, request: "PriceCompareRequest") -> PriceCompareResult:
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

    def compare(self, request: "PriceCompareRequest") -> PriceCompareResult:
        items = list(request.items)
        if not items:
            if not request.query:
                return _failed_price_result(
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
                return _failed_price_result(
                    provider=self.provider,
                    query=request.query,
                    code=error.code,
                    message=error.message,
                    recoverable=error.recoverable,
                )
            items = search_result.items

        return _compare_products(items, request, provider=self.provider)


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
    product_id = _clean_str(raw.get("itemid")) or _clean_str(raw.get("item_id"))
    if not product_id:
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
    url = _product_url(raw, product_id=product_id)

    style_tags: list[str] = []
    reason_parts: list[str] = []
    if coupon and coupon > 0:
        style_tags.append("coupon")
        reason_parts.append(f"含 {coupon:.0f} 元优惠券，券后价 {price:.2f}")
    if commission_rate:
        reason_parts.append(f"佣金比例 {commission_rate}")
    reason = "；".join(reason_parts) if reason_parts else None

    return ProductResult(
        product_id=product_id,
        title=title,
        category=_clean_str(raw.get("itemtitle_cat")) or None,
        price=price,
        platform=platform,
        shop=shop,
        url=url,
        product_url=url,
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


def _product_url(raw: dict[str, Any], *, product_id: str) -> str:
    direct = (
        _clean_str(raw.get("couponurl"))
        or _clean_str(raw.get("coupon_url"))
        or _clean_str(raw.get("coupon_click_url"))
        or _clean_str(raw.get("itemlink"))
        or _clean_str(raw.get("item_url"))
        or _clean_str(raw.get("itemurl"))
        or _clean_str(raw.get("url"))
    )
    if direct:
        return direct
    encoded_id = urllib.parse.quote(product_id, safe="")
    return f"https://item.taobao.com/item.htm?id={encoded_id}"


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


# Imported at module end to avoid a circular import with product_adapter, which
# imports nothing from this module at definition time.
from multimodal_agent.services.product_adapter import (  # noqa: E402
    PriceCompareRequest,
    ProductSearchRequest,
    _compare_products,
    _failed_price_result,
    _failed_search_result,
    _filter_products,
    _filters_used,
    _query_text,
)
