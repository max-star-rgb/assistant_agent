"""Build structured tool inputs from request and prior tool outputs."""

import re
from typing import Any

from assistant_agent.agent.prompt_builder import build_image_generation_request
from assistant_agent.agent.legacy_tool_mapping import canonical_capability_for_action
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.schemas.tool_ids import (
    IMAGE_GENERATION_CAPABILITY,
    IMAGE_UNDERSTANDING_TOOL_NAME,
    MEMORY_RETRIEVAL_CAPABILITY,
    MEMORY_SAVE_CAPABILITY,
    SHOPPING_SEARCH_CAPABILITY,
    SHOPPING_SEARCH_TOOL_NAME,
    VIDEO_UNDERSTANDING_CAPABILITY,
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
        return {
            "video_ref": request.video_ids[0] if request.video_ids else None,
            "video_ids": request.video_ids,
            "user_query": request.text,
            "user_id": request.user_id,
            "session_id": request.session_id,
            "metadata": _metadata_snapshot(request.metadata),
        }
    if action == "understand_image":
        return {"image_ids": request.image_ids, "video_ids": request.video_ids, "question": request.text}
    if capability == SHOPPING_SEARCH_CAPABILITY:
        visual = latest_visual_data(outputs_by_step)
        summary = visual.get("summary") if visual else None
        payload = {
            "query": request.text,
            "visual_summary": summary,
            "video_summary": summary if latest_video_data(outputs_by_step) else None,
            "objects": visual.get("objects", []) if visual else [],
            "colors": visual.get("colors", []) if visual else [],
            "materials": visual.get("materials", []) if visual else [],
        }
        return {key: value for key, value in payload.items() if value not in (None, "", [], {})}
    if capability == WEB_SEARCH_CAPABILITY:
        return build_web_search_input(request)
    if capability == WEB_FETCH_CAPABILITY:
        return build_web_fetch_input(request)
    if capability == IMAGE_GENERATION_CAPABILITY:
        return build_image_generation_request(request, outputs_by_step).model_dump()
    if capability == MEMORY_RETRIEVAL_CAPABILITY:
        return {"user_id": request.user_id, "query": request.text}
    if capability == MEMORY_SAVE_CAPABILITY:
        return {
            "user_id": request.user_id,
            "session_id": request.session_id,
            "query": request.text,
            "content": build_memory_save_content(request, outputs_by_step),
            "source_intent": "user_explicit",
            "source_reason": "兼容 rule plan 命中显式保存记忆动作。",
            "future_use": "后续任务可复用用户要求保存的信息。",
            "evidence": request.text or "compatibility save_memory action",
        }
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


def _metadata_snapshot(metadata: dict[str, Any]) -> dict[str, Any]:
    """Return a small copy safe to persist in tool call records."""

    return {
        key: value
        for key, value in metadata.items()
        if key not in {"assistant_loop_steps"} and isinstance(value, str | int | float | bool | list | dict | type(None))
    }


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
        if result.tool_name == IMAGE_UNDERSTANDING_TOOL_NAME and result.success and result.data:
            return result.data
    return {}


def latest_video_data(outputs_by_step: dict[str, ToolResult]) -> dict[str, Any]:
    """Return the latest video understanding data payload."""

    for result in reversed(list(outputs_by_step.values())):
        if _is_video_understanding_result(result) and result.success and result.data:
            return result.data
    return {}


def _is_video_understanding_result(result: ToolResult) -> bool:
    return bool(
        result.tool_name == IMAGE_UNDERSTANDING_TOOL_NAME
        and result.contract is not None
        and result.contract.capability == VIDEO_UNDERSTANDING_CAPABILITY
    )


def latest_visual_output_ref(outputs_by_step: dict[str, ToolResult]) -> str | None:
    """Return the latest visual/video understanding output ref."""

    for result in reversed(list(outputs_by_step.values())):
        if result.tool_name == IMAGE_UNDERSTANDING_TOOL_NAME and result.success:
            return result.output_ref
    return None


def build_memory_save_content(
    request: UserRequest,
    outputs_by_step: dict[str, ToolResult],
) -> dict[str, Any]:
    """Build a compact memory payload from user text and video context."""

    content: dict[str, Any] = {"text": request.text}
    video = latest_video_data(outputs_by_step)
    if video:
        for key in ("summary", "scene", "objects", "products", "colors", "materials", "style_tags"):
            value = video.get(key)
            if value:
                content[key] = value
        output_ref = latest_visual_output_ref(outputs_by_step)
        if output_ref:
            content["video_ref"] = output_ref
    return content
