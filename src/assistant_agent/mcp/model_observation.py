"""Tool-owned compact projections for high-volume MCP model observations."""

from __future__ import annotations

from typing import Any


_AMAP_SERVER_NAME = "amap_maps"
_AMAP_TEXT_SEARCH_TOOL_NAME = "maps_text_search"
_AMAP_POI_LIMIT = 5
_AMAP_POI_FIELDS = ("id", "name", "address", "location", "typecode")


def project_mcp_model_observation(
    *,
    server_name: str,
    tool_name: str,
    structured: Any,
) -> dict[str, Any] | None:
    """Return a bounded specialized view, or ``None`` for generic fallback."""

    if (
        server_name != _AMAP_SERVER_NAME
        or tool_name != _AMAP_TEXT_SEARCH_TOOL_NAME
        or not isinstance(structured, dict)
    ):
        return None
    pois = structured.get("pois")
    if not isinstance(pois, list):
        return None
    projected = [
        _project_amap_poi(poi)
        for poi in pois[:_AMAP_POI_LIMIT]
        if isinstance(poi, dict)
    ]
    return {
        "pois": projected,
        "total_count": len(pois),
        "returned_count": len(projected),
        "truncated": len(projected) < len(pois),
    }


def _project_amap_poi(poi: dict[str, Any]) -> dict[str, Any]:
    return {
        field: value
        for field in _AMAP_POI_FIELDS
        if (value := poi.get(field)) is not None
        and isinstance(value, (str, int, float, bool))
    }
