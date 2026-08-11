"""Stable cross-layer tool and capability protocol identifiers.

This module is not a tool registry or manifest. New plugin-local tools do not
need an entry here unless their identifier becomes a cross-layer protocol.
"""

DIRECT_CHAT_CAPABILITY = "direct_chat"
IMAGE_UNDERSTANDING_CAPABILITY = "image_understanding"
VIDEO_UNDERSTANDING_CAPABILITY = "video_understanding"
MEDIA_INSPECT_TOOL_NAME = "media_inspect"
LIVE_VIEW_INSPECT_TOOL_NAME = "live_view_inspect"
REALTIME_VIDEO_OBSERVE_TOOL_NAME = "realtime_video_observe"
VISUAL_MEMORY_SEARCH_TOOL_NAME = "visual_memory_search"
VISUAL_REMINDER_MANAGE_TOOL_NAME = "visual_reminder_manage"
# Deprecated import compatibility only; no tool is registered under the legacy name.
IMAGE_UNDERSTANDING_TOOL_NAME = MEDIA_INSPECT_TOOL_NAME
IMAGE_GENERATION_CAPABILITY = "image_generation"
IMAGE_GENERATION_TOOL_NAME = "image_generation"
WEB_SEARCH_CAPABILITY = "web_search"
WEB_SEARCH_TOOL_NAME = "web_search"
WEB_FETCH_CAPABILITY = "web_fetch"
WEB_FETCH_TOOL_NAME = "web_fetch"
VISUAL_IMAGE_SEARCH_CAPABILITY = "visual_image_search"
VISUAL_IMAGE_SEARCH_TOOL_NAME = "visual_image_search"
SHOPPING_SEARCH_CAPABILITY = "shopping_search"
SHOPPING_SEARCH_TOOL_NAME = "shopping_search"
CALENDAR_SEARCH_TOOL_NAME = "calendar_search"
CALENDAR_CREATE_TOOL_NAME = "calendar_create"
CONTACTS_SEARCH_TOOL_NAME = "contacts_search"
PYTHON_INTERPRETER_TOOL_NAME = "python_interpreter"
WORKFLOW_SUBMIT_TOOL_NAME = "workflow_submit"
HOTEL_PRICE_WATCH_CREATE_TOOL_NAME = "hotel_price_watch_create"
LOAD_SKILL_TOOL_NAME = "load_skill"
LOAD_SKILL_REFERENCE_TOOL_NAME = "load_skill_reference"
DURABLE_TASK_CREATION_TOOL_NAMES = frozenset(
    {
        HOTEL_PRICE_WATCH_CREATE_TOOL_NAME,
    }
)
