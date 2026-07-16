"""Deterministic App shopping-detail presentation."""

from __future__ import annotations

import re
from decimal import Decimal
from urllib.parse import urlparse

from assistant_agent.schemas.products import PriceCompareResult, PriceOffer, ShoppingSearchResult


_PROTOCOL_BLOCK_RE = re.compile(r"<(detail|link|pic)>.*?</\1>", re.IGNORECASE | re.DOTALL)
_PROTOCOL_TAG_RE = re.compile(r"</?(?:detail|link|pic)>", re.IGNORECASE)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_PLATFORM_LABELS = {"jd": "京东", "taobao": "淘宝", "tmall": "淘宝", "pdd": "拼多多"}
_PLATFORM_ORDER = ("jd", "taobao", "pdd")


class ShoppingDetailPresenter:
    """Render a successful structured price comparison for App clients."""

    def present(self, result: PriceCompareResult | ShoppingSearchResult) -> str:
        summary = _clean_summary(result.summary)
        offers = _offers(result)
        eligible = [offer for offer in offers if _eligible(offer)]
        if not eligible:
            return summary

        ordered: list[PriceOffer] = []
        best = _best_offer(result)
        if best is not None and _eligible(best):
            ordered.append(best)
        seen = {offer.offer_id for offer in ordered}
        for platform in _PLATFORM_ORDER:
            for offer in eligible:
                normalized = "taobao" if offer.platform == "tmall" else offer.platform
                if normalized == platform and offer.offer_id not in seen:
                    ordered.append(offer)
                    seen.add(offer.offer_id)

        lines = ["<detail>"]
        for index, offer in enumerate(ordered[:3], start=1):
            platform = "taobao" if offer.platform == "tmall" else offer.platform
            lines.append(
                f"{index}. {_PLATFORM_LABELS[platform]} - {_clean_title(offer.title)} "
                f"{_format_price(offer.total_price)}元 <link>{offer.product_url}</link> "
                f"<pic>{offer.image_url}</pic>"
            )
        lines.append("</detail>")
        block = "\n".join(lines)
        return f"{summary}\n{block}" if summary else block


def _offers(result: PriceCompareResult | ShoppingSearchResult) -> list[PriceOffer]:
    if result.offers:
        return list(result.offers)
    if isinstance(result, ShoppingSearchResult) and result.comparison is not None:
        return list(result.comparison.offers)
    return []


def _best_offer(result: PriceCompareResult | ShoppingSearchResult) -> PriceOffer | None:
    if result.best_offer is not None:
        return result.best_offer
    if isinstance(result, ShoppingSearchResult) and result.comparison is not None:
        return result.comparison.best_offer
    return None


def _clean_summary(value: str) -> str:
    cleaned = _PROTOCOL_BLOCK_RE.sub("", value or "")
    cleaned = _PROTOCOL_TAG_RE.sub("", cleaned)
    return _CONTROL_RE.sub(" ", cleaned).strip()


def _clean_title(value: str) -> str:
    cleaned = _PROTOCOL_TAG_RE.sub("", value)
    return " ".join(_CONTROL_RE.sub(" ", cleaned).split())


def _http_url(value: str | None) -> bool:
    if not value:
        return False
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _eligible(offer: PriceOffer) -> bool:
    return offer.total_price > 0 and _http_url(offer.product_url) and _http_url(offer.image_url)


def _format_price(value: float) -> str:
    decimal = Decimal(str(value)).quantize(Decimal("0.01"))
    return format(decimal, "f").rstrip("0").rstrip(".")
