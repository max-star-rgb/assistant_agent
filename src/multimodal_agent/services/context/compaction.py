"""Compact tool observations before they are rendered into model context."""

from __future__ import annotations

import json
from typing import Any, Mapping


MAX_ITEMS_PER_LIST = 3
MAX_TEXT_CHARS = 1200
MAX_GENERIC_DEPTH = 3

_TOP_LEVEL_KEYS = (
    "tool_name",
    "status",
    "summary",
    "output_ref",
    "structured_output",
    "error_code",
    "error_message",
    "next_step_hint",
    "redacted",
)

_PRODUCT_KEYS = (
    "product_id",
    "provider_item_id",
    "title",
    "brand",
    "category",
    "price",
    "currency",
    "platform",
    "shop",
    "url",
    "product_url",
    "url_status",
    "availability",
    "similarity",
    "similarity_score",
    "text_match_score",
    "rating",
    "sales",
    "material",
    "color",
    "style_tags",
    "reason",
    "source",
)

_OFFER_KEYS = (
    "offer_id",
    "product_id",
    "title",
    "platform",
    "shop",
    "price",
    "currency",
    "shipping_fee",
    "total_price",
    "product_url",
    "url_status",
    "availability",
    "rating",
    "sales",
    "similarity_score",
    "reason",
)


def compact_observations_for_context(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return assistant-facing compact observations without mutating originals."""

    return [compact_observation_for_context(observation) for observation in observations]


def compact_observation_for_context(observation: dict[str, Any]) -> dict[str, Any]:
    """Keep fields needed for ReAct decisions while trimming bulky payloads."""

    original_chars = _json_chars(observation)
    compacted: dict[str, Any] = {}
    for key in _TOP_LEVEL_KEYS:
        if key not in observation:
            continue
        value = observation[key]
        if key == "structured_output":
            compacted[key] = _compact_structured_output(str(observation.get("tool_name") or ""), value)
        elif isinstance(value, str):
            compacted[key] = _clip_text(value)
        else:
            compacted[key] = value

    compacted_chars = _json_chars(compacted)
    if compacted_chars < original_chars or set(observation) - set(compacted):
        compacted["compacted"] = True
        compacted["compaction"] = {
            "original_chars": original_chars,
            "compacted_chars": _json_chars(compacted),
            "max_items_per_list": MAX_ITEMS_PER_LIST,
            "max_text_chars": MAX_TEXT_CHARS,
        }
    return compacted


def _compact_structured_output(tool_name: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    data = dict(value)
    if tool_name == "product_search":
        return _compact_product_search_output(data)
    if tool_name == "price_compare":
        return _compact_price_compare_output(data)
    return _compact_generic_mapping(data)


def _compact_product_search_output(data: dict[str, Any]) -> dict[str, Any]:
    output = _copy_keys(
        data,
        (
            "provider",
            "query_used",
            "total",
            "filters_used",
            "summary",
            "output_ref",
            "errors",
            "latency_ms",
        ),
    )
    items = data.get("items")
    if isinstance(items, list):
        output["items"] = [_compact_product_item(item) for item in items[:MAX_ITEMS_PER_LIST] if isinstance(item, Mapping)]
        if len(items) > MAX_ITEMS_PER_LIST:
            output["omitted_items_count"] = len(items) - MAX_ITEMS_PER_LIST
    return _compact_generic_mapping(output)


def _compact_price_compare_output(data: dict[str, Any]) -> dict[str, Any]:
    output = _copy_keys(
        data,
        (
            "query",
            "summary",
            "provider",
            "best_value_product_id",
            "best_offer",
            "offers",
            "items",
            "ranking_reason",
            "errors",
            "latency_ms",
            "output_ref",
        ),
    )
    if isinstance(output.get("best_offer"), Mapping):
        output["best_offer"] = _compact_offer(output["best_offer"])
    offers = output.get("offers")
    if isinstance(offers, list):
        output["offers"] = [_compact_offer(offer) for offer in offers[:MAX_ITEMS_PER_LIST] if isinstance(offer, Mapping)]
        if len(offers) > MAX_ITEMS_PER_LIST:
            output["omitted_offers_count"] = len(offers) - MAX_ITEMS_PER_LIST
    items = output.get("items")
    if isinstance(items, list):
        output["items"] = [_compact_product_item(item) for item in items[:MAX_ITEMS_PER_LIST] if isinstance(item, Mapping)]
        if len(items) > MAX_ITEMS_PER_LIST:
            output["omitted_items_count"] = len(items) - MAX_ITEMS_PER_LIST
    return _compact_generic_mapping(output)


def _compact_product_item(item: Mapping[str, Any]) -> dict[str, Any]:
    return _compact_generic_mapping(_copy_keys(item, _PRODUCT_KEYS))


def _compact_offer(item: Mapping[str, Any]) -> dict[str, Any]:
    return _compact_generic_mapping(_copy_keys(item, _OFFER_KEYS))


def _copy_keys(data: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: data[key] for key in keys if key in data and data[key] is not None}


def _compact_generic_mapping(data: Mapping[str, Any], *, depth: int = 0) -> dict[str, Any]:
    compacted: dict[str, Any] = {}
    for key, value in data.items():
        if not isinstance(key, str):
            continue
        compacted[key] = _compact_generic_value(value, depth=depth + 1)
    return compacted


def _compact_generic_value(value: Any, *, depth: int) -> Any:
    if isinstance(value, str):
        return _clip_text(value)
    if isinstance(value, Mapping):
        if depth >= MAX_GENERIC_DEPTH:
            return _clip_text(json.dumps(dict(value), ensure_ascii=False, default=str))
        return _compact_generic_mapping(value, depth=depth)
    if isinstance(value, list):
        items = [_compact_generic_value(item, depth=depth + 1) for item in value[:MAX_ITEMS_PER_LIST]]
        return items
    return value


def _clip_text(value: str) -> str:
    if len(value) <= MAX_TEXT_CHARS:
        return value
    return value[: MAX_TEXT_CHARS - 20].rstrip() + "...[truncated]"


def _json_chars(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str))
