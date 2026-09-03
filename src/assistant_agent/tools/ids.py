"""Stable cross-layer tool and capability protocol identifiers.

This module is not a tool registry or manifest. New plugin-local tools do not
need an entry here unless their identifier becomes a cross-layer protocol.
"""

DIRECT_CHAT_CAPABILITY = "direct_chat"
IMAGE_UNDERSTANDING_CAPABILITY = "image_understanding"
VIDEO_UNDERSTANDING_CAPABILITY = "video_understanding"
UPLOADED_MEDIA_INSPECT_TOOL_NAME = "uploaded_media_inspect"
LIVE_VIEW_INSPECT_TOOL_NAME = "live_view_inspect"
VISUAL_MEMORY_SEARCH_TOOL_NAME = "visual_memory_search"
VISUAL_REMINDER_MANAGE_TOOL_NAME = "visual_reminder_manage"
IMAGE_GENERATION_CAPABILITY = "image_generation"
IMAGE_GENERATION_TOOL_NAME = "image_generation"
VISUAL_IMAGE_SEARCH_CAPABILITY = "visual_image_search"
VISUAL_IMAGE_SEARCH_TOOL_NAME = "visual_image_search"
SHOPPING_SEARCH_CAPABILITY = "shopping_search"
SHOPPING_SEARCH_TOOL_NAME = "shopping_search"
CALENDAR_SEARCH_TOOL_NAME = "calendar_search"
CALENDAR_CREATE_TOOL_NAME = "calendar_create"
CONTACTS_SEARCH_TOOL_NAME = "contacts_search"
HOTEL_PRICE_WATCH_CREATE_TOOL_NAME = "hotel_price_watch_create"
DURABLE_TASK_CREATION_TOOL_NAMES = frozenset(
    {
        HOTEL_PRICE_WATCH_CREATE_TOOL_NAME,
    }
)
