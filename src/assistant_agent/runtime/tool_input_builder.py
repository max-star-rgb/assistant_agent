"""Build structured tool inputs from request and prior tool outputs."""

import re
from typing import Any

from assistant_agent.runtime.prompt_builder import build_image_generation_request
from assistant_agent.runtime.legacy_tool_mapping import canonical_capability_for_action
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.ids import (
    IMAGE_GENERATION_CAPABILITY,
    LIVE_VIEW_INSPECT_TOOL_NAME,
    MEDIA_INSPECT_TOOL_NAME,
    SHOPPING_SEARCH_CAPABILITY,
    SHOPPING_SEARCH_TOOL_NAME,
    WEB_FETCH_CAPABILITY,
    WEB_SEARCH_CAPABILITY,
)


_URL_RE = re.compile(r"https?://\S+")


def build_tool_input(
    action: str,
    request: UserRequest,
    outputs_by_step: dict[str, ToolResult],
) -> dict[str, Any]:
    """Build the input payload for one planned tool action."""

    capability = canonical_capability_for_action(action) or action

    if action == "understand_video":
        return {"question": request.text}
    if action == "understand_image":
        return {"question": request.text}
    if capability == SHOPPING_SEARCH_CAPABILITY:
        return {"query": request.text}
    if capability == WEB_SEARCH_CAPABILITY:
        return build_web_search_input(request)
    if capability == WEB_FETCH_CAPABILITY:
        return build_web_fetch_input(request)
    if capability == IMAGE_GENERATION_CAPABILITY:
        generated = build_image_generation_request(request, outputs_by_step).model_dump()
        runtime_owned = {
            "user_id",
            "session_id",
            "memory_context",
            "prompt_extend",
            "watermark",
        }
        return {key: value for key, value in generated.items() if key not in runtime_owned}
    return {}


def build_web_search_input(request: UserRequest) -> dict[str, Any]:
    """Build web search input from a user request."""

    text = (request.text or "").strip()
    payload: dict[str, Any] = {"query": text}
    lowered = text.lower()
    if any(marker in text for marker in ("今天", "现在", "当前")) or any(
        marker in lowered for marker in ("today", "now", "current")
    ):
        payload["recency_days"] = 1
    elif any(marker in text for marker in ("最新", "最近")) or any(marker in lowered for marker in ("latest", "recent")):
        payload["recency_days"] = 7
    return payload


def build_web_fetch_input(request: UserRequest) -> dict[str, Any]:
    """Build web fetch input from a user request containing a URL."""

    match = _URL_RE.search(request.text or "")
    url = match.group(0).rstrip(".,，。)") if match else ""
    return {"url": url}


def latest_success_data(outputs_by_step: dict[str, ToolResult]) -> dict[str, Any]:
    """Return the latest successful tool data payload."""

    for result in reversed(list(outputs_by_step.values())):
        if result.data:
            return result.data
    return {}


def latest_items(outputs_by_step: dict[str, ToolResult]) -> list[dict[str, Any]]:
    """Return the latest list of product-like items from previous results."""

    for result in reversed(list(outputs_by_step.values())):
        if result.tool_name == SHOPPING_SEARCH_TOOL_NAME and result.data:
            search = result.data.get("search")
            if isinstance(search, dict) and isinstance(search.get("items"), list):
                return search["items"]
        if result.data and isinstance(result.data.get("items"), list):
            return result.data["items"]
    return []


def latest_visual_data(outputs_by_step: dict[str, ToolResult]) -> dict[str, Any]:
    """Return the latest visual/video understanding data payload."""

    for result in reversed(list(outputs_by_step.values())):
        if (
            result.tool_name
            in {MEDIA_INSPECT_TOOL_NAME, LIVE_VIEW_INSPECT_TOOL_NAME}
            and result.success
            and result.data
        ):
            return result.data
    return {}
