"""Deterministic shopping-card projection for the media wire."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

from langchain_core.messages import HumanMessage, ToolMessage
from pydantic import ValidationError

from assistant_agent.tools.ids import SHOPPING_SEARCH_TOOL_NAME
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


def shopping_detail_block(messages: Sequence[Any], *, max_items: int = 3) -> str:
    """Project the latest successful shopping artifact from the current turn."""

    result = _latest_shopping_result(messages)
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
        platform = _PLATFORM_LABELS.get(
            _clean_text(product.platform).casefold(),
            _clean_text(product.platform),
        )
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


def _latest_shopping_result(messages: Sequence[Any]) -> ShoppingSearchResult | None:
    for message in reversed(messages):
        if _is_human_message(message):
            break
        data = _message_data(message)
        if not _is_successful_shopping_message(message, data):
            continue
        artifact = data.get("artifact")
        if not isinstance(artifact, Mapping):
            return None
        try:
            return ShoppingSearchResult.model_validate(artifact)
        except ValidationError:
            return None
    return None


def _message_data(message: Any) -> Mapping[str, Any]:
    if isinstance(message, Mapping):
        return message
    if hasattr(message, "model_dump"):
        data = message.model_dump()
        return data if isinstance(data, Mapping) else {}
    return {}


def _is_human_message(message: Any) -> bool:
    if isinstance(message, HumanMessage):
        return True
    data = _message_data(message)
    return data.get("type") == "human" or data.get("role") == "user"


def _is_successful_shopping_message(
    message: Any,
    data: Mapping[str, Any],
) -> bool:
    if isinstance(message, ToolMessage):
        name = message.name
        status = message.status
    else:
        name = data.get("name")
        status = data.get("status")
        if data.get("type") not in {"tool", "ToolMessage"}:
            return False
    return name == SHOPPING_SEARCH_TOOL_NAME and status in {None, "success"}


def _clean_text(value: str) -> str:
    without_tags = _PROTOCOL_TAG_RE.sub("", value)
    without_controls = _CONTROL_RE.sub(" ", without_tags)
    return _SPACE_RE.sub(" ", without_controls).strip()[:240]


def _safe_http_url(value: str | None) -> bool:
    if not isinstance(value, str):
        return False
    if value != value.strip() or any(
        character.isspace() or character in "<>" for character in value
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


__all__ = ["shopping_detail_block"]
