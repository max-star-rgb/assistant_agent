"""Deterministic shopping-detail projection for capable delivery entries."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable
from urllib.parse import urlsplit

from pydantic import ValidationError

from assistant_agent.tools.ids import SHOPPING_SEARCH_TOOL_NAME
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.plugins.builtin.shopping.models import ShoppingSearchResult


_PROTOCOL_TAG_RE = re.compile(
    r"</?(?:detail|link|pic)(?:\s[^>]*)?>",
    flags=re.IGNORECASE,
)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_SPACE_RE = re.compile(r"\s+")
_PLATFORM_LABELS = {
    "jd": "京东",
    "jingdong": "京东",
    "taobao": "淘宝",
    "tmall": "天猫",
    "pdd": "拼多多",
}


def project_shopping_delivery_text(
    response_text: str,
    tool_results: Iterable[ToolResult],
    *,
    metadata: dict[str, Any],
) -> tuple[str, str]:
    """Append detail only for entries that explicitly support the protocol."""

    if not shopping_detail_enabled(metadata):
        return response_text, ""
    detail = shopping_detail_block(tool_results)
    if not detail:
        return response_text, ""
    separator = "\n" if response_text else ""
    return f"{response_text}{separator}{detail}", detail


def shopping_detail_block(tool_results: Iterable[ToolResult], *, max_items: int = 3) -> str:
    """Project the last successful shopping result into the legacy detail protocol."""

    result = _last_successful_shopping_result(tool_results)
    if result is None:
        return ""

    lines: list[str] = []
    for selection in result.selections:
        product = selection.product
        product_url = product.product_url or product.url
        image_url = product.image_url
        if not _safe_http_url(product_url) or not _safe_http_url(image_url):
            continue
        title = _clean_text(product.title)
        platform = _platform_label(product.platform)
        price = _format_price(selection.subtotal)
        if not title or not platform or price is None:
            continue
        lines.append(
            f"{len(lines) + 1}. {platform} - {title} {price}元 "
            f"<link>{product_url}</link><pic>{image_url}</pic>"
        )
        if len(lines) >= max_items:
            break
    if not lines:
        return ""
    return "<detail>\n" + "\n".join(lines) + "\n</detail>"


def shopping_detail_enabled(metadata: dict[str, Any]) -> bool:
    """Return whether the normalized entry explicitly supports detail v1."""

    gateway = metadata.get("gateway")
    if not isinstance(gateway, dict):
        return False
    capabilities = gateway.get("entry_capabilities")
    return (
        isinstance(capabilities, dict)
        and capabilities.get("supports_shopping_detail_v1") is True
    )


def _last_successful_shopping_result(
    tool_results: Iterable[ToolResult],
) -> ShoppingSearchResult | None:
    for tool_result in reversed(list(tool_results)):
        if (
            tool_result.tool_name != SHOPPING_SEARCH_TOOL_NAME
            or not tool_result.success
            or not isinstance(tool_result.data, dict)
        ):
            continue
        try:
            return ShoppingSearchResult.model_validate(tool_result.data)
        except ValidationError:
            return None
    return None


def _clean_text(value: str) -> str:
    without_tags = _PROTOCOL_TAG_RE.sub("", value)
    without_controls = _CONTROL_RE.sub(" ", without_tags)
    return _SPACE_RE.sub(" ", without_controls).strip()[:240]


def _platform_label(value: str) -> str:
    normalized = _clean_text(value)
    return _PLATFORM_LABELS.get(normalized.casefold(), normalized)


def _safe_http_url(value: str | None) -> bool:
    if not isinstance(value, str):
        return False
    if value != value.strip() or any(
        character.isspace() or character in "<>"
        for character in value
    ):
        return False
    parsed = urlsplit(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _format_price(value: float) -> str | None:
    try:
        price = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not price.is_finite() or price < 0:
        return None
    formatted = format(price.quantize(Decimal("0.01")), "f")
    return formatted.rstrip("0").rstrip(".")
