"""Safe local-only content projection for visual Tool diagnostics."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_BLOCKED_VISUAL_TRACE_CONTENT_KEYS = frozenset(
    {
        "embedding",
        "evidence_ref",
        "frame_ref",
        "frame_refs",
        "image_id",
        "image_ids",
        "media_ref",
        "media_refs",
        "path",
        "provider_payload",
        "provider_raw_response",
        "raw",
        "raw_content",
        "raw_data",
        "raw_output",
        "raw_payload",
        "raw_provider_payload",
        "raw_provider_response",
        "raw_response",
        "search_embedding",
        "uri",
        "vector",
        "video_id",
        "video_ids",
    }
)
_SAFE_VISUAL_TOOL_RESULT_DATA_KEYS = frozenset(
    {
        "actions",
        "brands",
        "colors",
        "confidence",
        "description",
        "error_code",
        "events",
        "fallback_used",
        "freshness",
        "in_flight",
        "keyframe_count",
        "latency_ms",
        "materials",
        "model",
        "objects",
        "observations",
        "observed_timestamp_ms",
        "pending_count",
        "people",
        "products",
        "provider",
        "scene",
        "sequence_gap",
        "snapshot_sequence",
        "source",
        "status",
        "style_tags",
        "summary",
        "target_sequence",
        "text_in_video",
        "timestamps",
        "usable_visual_text",
    }
)


def sanitize_visual_trace_content(value: Any) -> Any:
    """Remove media identity, evidence, vector, and Provider-raw fields."""

    if isinstance(value, dict):
        return {
            key: sanitize_visual_trace_content(nested)
            for key, nested in value.items()
            if isinstance(key, str)
            and key.strip().lower() not in _BLOCKED_VISUAL_TRACE_CONTENT_KEYS
        }
    if isinstance(value, list | tuple):
        return [sanitize_visual_trace_content(item) for item in value]
    return value


def sanitize_visual_tool_result(result: Mapping[str, Any]) -> dict[str, Any]:
    """Allowlist one local visual ToolResult without media or Provider raw data."""

    raw_data = result.get("data")
    data = (
        {
            key: sanitize_visual_trace_content(value)
            for key, value in raw_data.items()
            if key in _SAFE_VISUAL_TOOL_RESULT_DATA_KEYS
        }
        if isinstance(raw_data, Mapping)
        else {}
    )
    return {
        key: value
        for key, value in {
            "tool_name": result.get("tool_name"),
            "success": result.get("success"),
            "output_ref": result.get("output_ref"),
            "data": data,
            "error": sanitize_visual_trace_content(result.get("error")),
        }.items()
        if value not in (None, "", [], {})
    }
