"""Compact tool observations before they are rendered into model context."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from assistant_agent.tools.ids import SHOPPING_SEARCH_TOOL_NAME


MAX_ITEMS_PER_LIST = 3
MAX_TEXT_CHARS = 1200
MAX_GENERIC_DEPTH = 3
MAX_COMMAND_OUTPUT_LINES = 20
MAX_COMMAND_OUTPUT_CHARS = 1200
MAX_PRUNED_KEYS_IN_METADATA = 20
PRUNED_INLINE_MEDIA_PLACEHOLDER = "[pruned inline media payload]"

_TOP_LEVEL_KEYS = (
    "tool_name",
    "status",
    "summary",
    "output_ref",
    "artifact_ref",
    "artifact_refs",
    "file_ref",
    "file_refs",
    "image_ref",
    "image_refs",
    "video_ref",
    "video_refs",
    "media_ref",
    "media_refs",
    "structured_output",
    "error_code",
    "error_message",
    "next_step_hint",
    "redacted",
    "truncated",
    "original_chars",
)

_RAW_PAYLOAD_KEYS = {
    "audio_base64",
    "audio_bytes",
    "audio_data",
    "base64",
    "binary",
    "blob",
    "bytes",
    "data_uri",
    "data_url",
    "file_base64",
    "file_bytes",
    "file_content",
    "file_contents",
    "file_data",
    "http_response_body",
    "image_base64",
    "image_bytes",
    "image_data",
    "media_base64",
    "media_body",
    "media_bytes",
    "media_data",
    "provider_payload",
    "provider_raw_payload",
    "provider_raw_response",
    "provider_response",
    "raw",
    "raw_audio",
    "raw_body",
    "raw_content",
    "raw_data",
    "raw_file",
    "raw_html",
    "raw_image",
    "raw_media",
    "raw_output",
    "raw_payload",
    "raw_provider_payload",
    "raw_provider_response",
    "raw_result",
    "raw_results",
    "raw_video",
    "response_body",
    "video_base64",
    "video_bytes",
    "video_data",
}

_COMMAND_OUTPUT_KEYS = {
    "command_output",
    "console_output",
    "logs",
    "process_output",
    "shell_output",
    "stderr",
    "stdout",
    "terminal_output",
}

_SECRET_PAYLOAD_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "authorization",
    "client_secret",
    "cookie",
    "password",
    "refresh_token",
    "secret",
    "secret_token",
}

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


@dataclass
class _CompactionStats:
    pruned_keys: list[str] = field(default_factory=list)
    command_output_keys: list[str] = field(default_factory=list)
    original_command_output_chars: int = 0
    compacted_command_output_chars: int = 0

    def record_pruned_key(self, key_path: tuple[str, ...]) -> None:
        formatted = _format_key_path(key_path)
        if formatted not in self.pruned_keys:
            self.pruned_keys.append(formatted)

    def record_command_output(self, key_path: tuple[str, ...], *, original_chars: int, compacted_chars: int) -> None:
        formatted = _format_key_path(key_path)
        if formatted not in self.command_output_keys:
            self.command_output_keys.append(formatted)
        self.original_command_output_chars += max(0, original_chars)
        self.compacted_command_output_chars += max(0, compacted_chars)

    @property
    def changed(self) -> bool:
        return bool(self.pruned_keys or self.command_output_keys)


def compact_observations_for_context(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return assistant-facing compact observations without mutating originals."""

    return [compact_observation_for_context(observation) for observation in observations]


def sanitize_observations_for_context(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove unsafe payload fields without applying size or item-count limits."""

    return [_sanitize_observation_for_context(observation) for observation in observations]


def _sanitize_observation_for_context(observation: dict[str, Any]) -> dict[str, Any]:
    pruned_keys: list[str] = []

    def sanitize(value: Any, key_path: tuple[str, ...] = ()) -> Any:
        if isinstance(value, Mapping):
            sanitized: dict[str, Any] = {}
            for key, nested in value.items():
                if not isinstance(key, str):
                    continue
                child_path = (*key_path, key)
                if _should_prune_payload_key(key, nested):
                    pruned_keys.append(_format_key_path(child_path))
                    continue
                sanitized[key] = sanitize(nested, child_path)
            return sanitized
        if isinstance(value, list):
            return [sanitize(item, (*key_path, f"[{index}]")) for index, item in enumerate(value)]
        if isinstance(value, str) and _looks_like_inline_media_payload(value):
            pruned_keys.append(_format_key_path(key_path))
            return PRUNED_INLINE_MEDIA_PLACEHOLDER
        return value

    sanitized = sanitize(observation)
    if pruned_keys:
        sanitized["redacted"] = True
        sanitized["sanitization"] = {
            "pruned_keys": list(dict.fromkeys(pruned_keys)),
            "size_limits_applied": False,
        }
    return sanitized


def compact_observation_for_context(observation: dict[str, Any]) -> dict[str, Any]:
    """Keep fields needed for ReAct decisions while trimming bulky payloads."""

    original_chars = _json_chars(observation)
    stats = _CompactionStats()
    compacted: dict[str, Any] = {}
    for key, value in observation.items():
        if key not in _TOP_LEVEL_KEYS and _should_prune_payload_key(key, value):
            stats.record_pruned_key((key,))
    for key in _TOP_LEVEL_KEYS:
        if key not in observation:
            continue
        value = observation[key]
        if key == "structured_output":
            compacted[key] = _compact_structured_output(
                str(observation.get("tool_name") or ""),
                value,
                stats=stats,
            )
        elif isinstance(value, str):
            compacted[key] = _clip_text(value) if not _looks_like_inline_media_payload(value) else PRUNED_INLINE_MEDIA_PLACEHOLDER
            if compacted[key] == PRUNED_INLINE_MEDIA_PLACEHOLDER:
                stats.record_pruned_key((key,))
        else:
            compacted[key] = value

    compacted_chars = _json_chars(compacted)
    if compacted_chars < original_chars or set(observation) - set(compacted) or stats.changed:
        compacted["compacted"] = True
        compaction_metadata: dict[str, Any] = {
            "original_chars": original_chars,
            "compacted_chars": _json_chars(compacted),
            "max_items_per_list": MAX_ITEMS_PER_LIST,
            "max_text_chars": MAX_TEXT_CHARS,
        }
        if stats.pruned_keys:
            compaction_metadata["pruned_keys"] = stats.pruned_keys[:MAX_PRUNED_KEYS_IN_METADATA]
            omitted = len(stats.pruned_keys) - MAX_PRUNED_KEYS_IN_METADATA
            if omitted > 0:
                compaction_metadata["omitted_pruned_keys_count"] = omitted
        if stats.command_output_keys:
            compaction_metadata["command_output_keys"] = stats.command_output_keys[:MAX_PRUNED_KEYS_IN_METADATA]
            omitted = len(stats.command_output_keys) - MAX_PRUNED_KEYS_IN_METADATA
            if omitted > 0:
                compaction_metadata["omitted_command_output_keys_count"] = omitted
            compaction_metadata["max_command_output_lines"] = MAX_COMMAND_OUTPUT_LINES
            compaction_metadata["max_command_output_chars"] = MAX_COMMAND_OUTPUT_CHARS
            compaction_metadata["original_command_output_chars"] = stats.original_command_output_chars
            compaction_metadata["compacted_command_output_chars"] = stats.compacted_command_output_chars
        compacted["compaction"] = compaction_metadata
    return compacted


def _compact_structured_output(tool_name: str, value: Any, *, stats: _CompactionStats) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    data = dict(value)
    if tool_name == SHOPPING_SEARCH_TOOL_NAME:
        return _compact_shopping_search_output(data, stats=stats)
    return _compact_generic_mapping(data, stats=stats, key_path=("structured_output",))


def _compact_shopping_search_search_output(data: dict[str, Any], *, stats: _CompactionStats) -> dict[str, Any]:
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
        output["items"] = [
            _compact_product_item(item, stats=stats, key_path=("structured_output", "items", f"[{index}]"))
            for index, item in enumerate(items[:MAX_ITEMS_PER_LIST])
            if isinstance(item, Mapping)
        ]
        if len(items) > MAX_ITEMS_PER_LIST:
            output["omitted_items_count"] = len(items) - MAX_ITEMS_PER_LIST
    return _compact_generic_mapping(output, stats=stats, key_path=("structured_output",))


def _compact_shopping_search_comparison_output(data: dict[str, Any], *, stats: _CompactionStats) -> dict[str, Any]:
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
        output["best_offer"] = _compact_offer(
            output["best_offer"],
            stats=stats,
            key_path=("structured_output", "best_offer"),
        )
    offers = output.get("offers")
    if isinstance(offers, list):
        output["offers"] = [
            _compact_offer(offer, stats=stats, key_path=("structured_output", "offers", f"[{index}]"))
            for index, offer in enumerate(offers[:MAX_ITEMS_PER_LIST])
            if isinstance(offer, Mapping)
        ]
        if len(offers) > MAX_ITEMS_PER_LIST:
            output["omitted_offers_count"] = len(offers) - MAX_ITEMS_PER_LIST
    items = output.get("items")
    if isinstance(items, list):
        output["items"] = [
            _compact_product_item(item, stats=stats, key_path=("structured_output", "items", f"[{index}]"))
            for index, item in enumerate(items[:MAX_ITEMS_PER_LIST])
            if isinstance(item, Mapping)
        ]
        if len(items) > MAX_ITEMS_PER_LIST:
            output["omitted_items_count"] = len(items) - MAX_ITEMS_PER_LIST
    return _compact_generic_mapping(output, stats=stats, key_path=("structured_output",))


def _compact_shopping_search_output(data: dict[str, Any], *, stats: _CompactionStats) -> dict[str, Any]:
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
    search = data.get("search")
    if isinstance(search, Mapping):
        output["search"] = _compact_shopping_search_search_output(dict(search), stats=stats)
    comparison = data.get("comparison")
    if isinstance(comparison, Mapping):
        output["comparison"] = _compact_shopping_search_comparison_output(dict(comparison), stats=stats)
    if isinstance(output.get("best_offer"), Mapping):
        output["best_offer"] = _compact_offer(
            output["best_offer"],
            stats=stats,
            key_path=("structured_output", "best_offer"),
        )
    offers = output.get("offers")
    if isinstance(offers, list):
        output["offers"] = [
            _compact_offer(offer, stats=stats, key_path=("structured_output", "offers", f"[{index}]"))
            for index, offer in enumerate(offers[:MAX_ITEMS_PER_LIST])
            if isinstance(offer, Mapping)
        ]
        if len(offers) > MAX_ITEMS_PER_LIST:
            output["omitted_offers_count"] = len(offers) - MAX_ITEMS_PER_LIST
    items = output.get("items")
    if isinstance(items, list):
        output["items"] = [
            _compact_product_item(item, stats=stats, key_path=("structured_output", "items", f"[{index}]"))
            for index, item in enumerate(items[:MAX_ITEMS_PER_LIST])
            if isinstance(item, Mapping)
        ]
        if len(items) > MAX_ITEMS_PER_LIST:
            output["omitted_items_count"] = len(items) - MAX_ITEMS_PER_LIST
    return _compact_generic_mapping(output, stats=stats, key_path=("structured_output",))


def _compact_product_item(
    item: Mapping[str, Any],
    *,
    stats: _CompactionStats,
    key_path: tuple[str, ...],
) -> dict[str, Any]:
    _record_pruned_payload_keys(item, stats=stats, key_path=key_path)
    return _compact_generic_mapping(_copy_keys(item, _PRODUCT_KEYS), stats=stats, key_path=key_path)


def _compact_offer(item: Mapping[str, Any], *, stats: _CompactionStats, key_path: tuple[str, ...]) -> dict[str, Any]:
    _record_pruned_payload_keys(item, stats=stats, key_path=key_path)
    return _compact_generic_mapping(_copy_keys(item, _OFFER_KEYS), stats=stats, key_path=key_path)


def _copy_keys(data: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: data[key] for key in keys if key in data and data[key] is not None}


def _compact_generic_mapping(
    data: Mapping[str, Any],
    *,
    depth: int = 0,
    stats: _CompactionStats,
    key_path: tuple[str, ...],
) -> dict[str, Any]:
    compacted: dict[str, Any] = {}
    for key, value in data.items():
        if not isinstance(key, str):
            continue
        child_path = (*key_path, key)
        if _should_prune_payload_key(key, value):
            stats.record_pruned_key(child_path)
            continue
        if _is_command_output_key(key):
            compacted[key] = _compact_command_output(value, stats=stats, key_path=child_path)
            continue
        compacted_value = _compact_generic_value(value, depth=depth + 1, stats=stats, key_path=child_path)
        compacted[key] = compacted_value
    return compacted


def _compact_generic_value(value: Any, *, depth: int, stats: _CompactionStats, key_path: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        if _looks_like_inline_media_payload(value):
            stats.record_pruned_key(key_path)
            return PRUNED_INLINE_MEDIA_PLACEHOLDER
        return _clip_text(value)
    if isinstance(value, Mapping):
        if depth >= MAX_GENERIC_DEPTH:
            return _summarize_deep_mapping(value, stats=stats, key_path=key_path)
        return _compact_generic_mapping(value, depth=depth, stats=stats, key_path=key_path)
    if isinstance(value, list):
        items = [
            _compact_generic_value(item, depth=depth + 1, stats=stats, key_path=(*key_path, f"[{index}]"))
            for index, item in enumerate(value[:MAX_ITEMS_PER_LIST])
        ]
        return items
    return value


def _compact_command_output(value: Any, *, stats: _CompactionStats, key_path: tuple[str, ...]) -> str:
    original_chars = _json_chars(value)
    text = _command_output_text(value, stats=stats, key_path=key_path)
    lines = text.splitlines()
    if not lines and text:
        lines = [text]

    omitted_lines = max(0, len(lines) - MAX_COMMAND_OUTPUT_LINES)
    compacted = "\n".join(lines[:MAX_COMMAND_OUTPUT_LINES])
    truncated_chars = False
    if len(compacted) > MAX_COMMAND_OUTPUT_CHARS:
        compacted = compacted[: MAX_COMMAND_OUTPUT_CHARS - 29].rstrip() + "\n...[command output truncated]"
        truncated_chars = True
    if omitted_lines > 0:
        suffix = f"\n...[{omitted_lines} lines truncated]"
        if len(compacted) + len(suffix) > MAX_COMMAND_OUTPUT_CHARS:
            available = max(0, MAX_COMMAND_OUTPUT_CHARS - len(suffix))
            compacted = compacted[:available].rstrip()
            truncated_chars = True
        compacted += suffix
    if omitted_lines > 0 or truncated_chars or len(compacted) < original_chars:
        stats.record_command_output(
            key_path,
            original_chars=original_chars,
            compacted_chars=len(compacted),
        )
    return compacted


def _command_output_text(value: Any, *, stats: _CompactionStats, key_path: tuple[str, ...]) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(not isinstance(item, (Mapping, list)) for item in value):
        return "\n".join(str(item) for item in value)
    safe_value = _compact_generic_value(value, depth=0, stats=stats, key_path=key_path)
    return json.dumps(safe_value, ensure_ascii=False, default=str)


def _summarize_deep_mapping(
    value: Mapping[str, Any],
    *,
    stats: _CompactionStats,
    key_path: tuple[str, ...],
) -> dict[str, Any]:
    safe_keys: list[str] = []
    pruned_count = 0
    for key, item in value.items():
        if not isinstance(key, str):
            continue
        if _should_prune_payload_key(key, item):
            stats.record_pruned_key((*key_path, key))
            pruned_count += 1
            continue
        safe_keys.append(key)
    summary: dict[str, Any] = {"type": "mapping", "keys": safe_keys[:MAX_ITEMS_PER_LIST]}
    if len(safe_keys) > MAX_ITEMS_PER_LIST:
        summary["omitted_keys_count"] = len(safe_keys) - MAX_ITEMS_PER_LIST
    if pruned_count:
        summary["pruned_payload_keys_count"] = pruned_count
    return summary


def _record_pruned_payload_keys(
    data: Mapping[str, Any],
    *,
    stats: _CompactionStats,
    key_path: tuple[str, ...],
) -> None:
    for key, value in data.items():
        if isinstance(key, str) and _should_prune_payload_key(key, value):
            stats.record_pruned_key((*key_path, key))


def _should_prune_payload_key(key: str, value: Any) -> bool:
    normalized = _normalize_key(key)
    if normalized in _SECRET_PAYLOAD_KEYS or normalized.endswith(("_api_key", "_password", "_secret", "_token")):
        return True
    if normalized in _RAW_PAYLOAD_KEYS:
        return True
    if normalized.startswith("raw_"):
        return True
    if normalized.endswith(("_base64", "_bytes", "_blob", "_data_uri")):
        return True
    if "base64" in normalized:
        return True
    if "provider_response" in normalized or "provider_payload" in normalized:
        return True
    if normalized.endswith("html") and _json_chars(value) > 200:
        return True
    if isinstance(value, str) and _looks_like_inline_media_payload(value):
        return True
    return False


def _is_command_output_key(key: str) -> bool:
    return _normalize_key(key) in _COMMAND_OUTPUT_KEYS


def _looks_like_inline_media_payload(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    prefix = value[:80].strip().lower()
    return prefix.startswith(("data:image/", "data:video/", "data:audio/", "data:application/octet-stream"))


def _normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", key.strip().lower()).strip("_")


def _format_key_path(parts: tuple[str, ...]) -> str:
    formatted = ""
    for part in parts:
        if part.startswith("["):
            formatted += part
        else:
            formatted += f".{part}" if formatted else part
    return formatted


def _clip_text(value: str) -> str:
    if len(value) <= MAX_TEXT_CHARS:
        return value
    return value[: MAX_TEXT_CHARS - 20].rstrip() + "...[truncated]"


def _json_chars(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str))
