"""Closed identifiers for structured runtime Tool availability."""

from __future__ import annotations

from enum import Enum

from langchain_core.tools import BaseTool


class ToolAvailability(str, Enum):
    ALWAYS = "always"
    UPLOADED_MEDIA_PRESENT = "uploaded_media_present"
    VIDEO_FRAME_RECEIVED = "video_frame_received"
    VISUAL_HISTORY_AVAILABLE = "visual_history_available"


def tool_availability(tool: BaseTool) -> ToolAvailability:
    raw = (tool.metadata or {}).get(
        "availability",
        ToolAvailability.ALWAYS.value,
    )
    return ToolAvailability(raw)


__all__ = ["ToolAvailability", "tool_availability"]
