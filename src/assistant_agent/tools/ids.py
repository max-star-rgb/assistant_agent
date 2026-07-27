"""Stable cross-layer tool and capability protocol identifiers.

This module is not a tool registry or manifest. New plugin-local tools do not
need an entry here unless their identifier becomes a cross-layer protocol.
"""

DIRECT_CHAT_CAPABILITY = "direct_chat"
MULTI_STEP_ORCHESTRATION_CAPABILITY = "multi_step_orchestration"
ASK_FOLLOWUP_CAPABILITY = "ask_followup"
IMAGE_UNDERSTANDING_CAPABILITY = "image_understanding"
IMAGE_UNDERSTANDING_TOOL_NAME = "vision_understanding"
VIDEO_UNDERSTANDING_CAPABILITY = "video_understanding"
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
WEATHER_TOOL_NAME = "weather"
CALENDAR_SEARCH_TOOL_NAME = "calendar_search"
CALENDAR_CREATE_TOOL_NAME = "calendar_create"
CONTACTS_SEARCH_TOOL_NAME = "contacts_search"
PYTHON_INTERPRETER_TOOL_NAME = "python_interpreter"
TASK_PLAN_SUBMIT_TOOL_NAME = "task_plan_submit"
HOTEL_PRICE_WATCH_CREATE_TOOL_NAME = "hotel_price_watch_create"
DURABLE_TASK_SUBMISSION_TOOL_NAMES = frozenset(
    {
        TASK_PLAN_SUBMIT_TOOL_NAME,
        HOTEL_PRICE_WATCH_CREATE_TOOL_NAME,
    }
)
