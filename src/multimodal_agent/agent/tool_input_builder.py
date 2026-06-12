"""Build structured tool inputs from request and prior tool outputs."""

from typing import Any

from multimodal_agent.schemas.requests import UserRequest
from multimodal_agent.schemas.tools import ToolResult


def build_tool_input(
    action: str,
    request: UserRequest,
    outputs_by_step: dict[str, ToolResult],
) -> dict[str, Any]:
    """Build the input payload for one planned tool action."""

    if action in {"understand_image", "understand_video"}:
        return {"image_ids": request.image_ids, "video_ids": request.video_ids, "question": request.text}
    if action == "search_product":
        visual = latest_success_data(outputs_by_step)
        return {"query": request.text, "visual_summary": visual.get("summary") if visual else None}
    if action == "compare_price":
        return {"query": request.text or "白色低帮运动鞋", "items": latest_items(outputs_by_step)}
    if action == "generate_image":
        products = latest_items(outputs_by_step)
        first_product = products[0] if products else {}
        return {
            "prompt": request.text,
            "style": "日系海报",
            "product_title": first_product.get("title"),
            "product_id": first_product.get("product_id"),
        }
    if action == "render_3d":
        return {"product_id": "p1", "scene": request.text}
    if action == "retrieve_memory":
        return {"action": "retrieve", "user_id": request.user_id, "query": request.text}
    if action == "save_memory":
        return {"action": "save", "user_id": request.user_id, "content": {"text": request.text}}
    return {}


def latest_success_data(outputs_by_step: dict[str, ToolResult]) -> dict[str, Any]:
    """Return the latest successful tool data payload."""

    for result in reversed(list(outputs_by_step.values())):
        if result.data:
            return result.data
    return {}


def latest_items(outputs_by_step: dict[str, ToolResult]) -> list[dict[str, Any]]:
    """Return the latest list of product-like items from previous results."""

    for result in reversed(list(outputs_by_step.values())):
        if result.data and isinstance(result.data.get("items"), list):
            return result.data["items"]
    return []
