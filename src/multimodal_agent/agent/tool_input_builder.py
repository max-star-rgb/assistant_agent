"""Build structured tool inputs from request and prior tool outputs."""

from typing import Any

from multimodal_agent.agent.prompt_builder import build_image_generation_request
from multimodal_agent.schemas.requests import UserRequest
from multimodal_agent.schemas.tools import ToolResult


def build_tool_input(
    action: str,
    request: UserRequest,
    outputs_by_step: dict[str, ToolResult],
) -> dict[str, Any]:
    """Build the input payload for one planned tool action."""

    if action == "understand_video":
        return {
            "video_ref": request.video_ids[0] if request.video_ids else None,
            "user_query": request.text,
            "user_id": request.user_id,
            "session_id": request.session_id,
            "metadata": _metadata_snapshot(request.metadata),
        }
    if action == "understand_image":
        return {"image_ids": request.image_ids, "video_ids": request.video_ids, "question": request.text}
    if action == "search_product":
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
    if action == "compare_price":
        return {"query": request.text or "白色低帮运动鞋", "items": latest_items(outputs_by_step)}
    if action == "generate_image":
        return build_image_generation_request(request, outputs_by_step).model_dump()
    if action == "render_3d":
        return build_render_request_input(request, outputs_by_step)
    if action == "retrieve_memory":
        return {"action": "retrieve", "user_id": request.user_id, "query": request.text}
    if action == "save_memory":
        return {
            "action": "save",
            "user_id": request.user_id,
            "session_id": request.session_id,
            "query": request.text,
            "content": build_memory_save_content(request, outputs_by_step),
        }
    return {}


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
        if result.data and isinstance(result.data.get("items"), list):
            return result.data["items"]
    return []


def latest_output_ref(outputs_by_step: dict[str, ToolResult]) -> str | None:
    """Return the latest successful output reference."""

    for result in reversed(list(outputs_by_step.values())):
        if result.success and result.output_ref:
            return result.output_ref
    return None


def build_render_request_input(
    request: UserRequest,
    outputs_by_step: dict[str, ToolResult],
) -> dict[str, Any]:
    """Build a RenderRequest payload from text and previous tool outputs."""

    payload: dict[str, Any] = {
        "scene_description": request.text,
        "scene": request.text,
        "user_id": request.user_id,
        "session_id": request.session_id,
    }

    products = latest_items(outputs_by_step)
    if products:
        _merge_product_render_context(payload, products[0])

    visual = latest_visual_data(outputs_by_step)
    if visual:
        _merge_visual_render_context(payload, visual, latest_visual_output_ref(outputs_by_step))

    memory_items = latest_memory_items(outputs_by_step)
    if memory_items:
        _merge_memory_render_context(payload, memory_items)
    else:
        summaries = request.metadata.get("memory_context_summaries")
        if isinstance(summaries, list):
            payload["memory_context"] = [summary for summary in summaries if isinstance(summary, str)]

    output_ref = latest_output_ref(outputs_by_step)
    if output_ref and not payload.get("image_ref") and not payload.get("video_ref"):
        payload["image_ref"] = output_ref

    return {key: value for key, value in payload.items() if value not in (None, "", [], {})}


def latest_visual_data(outputs_by_step: dict[str, ToolResult]) -> dict[str, Any]:
    """Return the latest visual/video understanding data payload."""

    for result in reversed(list(outputs_by_step.values())):
        if result.tool_name in {"vision_understanding", "video_understanding"} and result.success and result.data:
            return result.data
    return {}


def latest_video_data(outputs_by_step: dict[str, ToolResult]) -> dict[str, Any]:
    """Return the latest video understanding data payload."""

    for result in reversed(list(outputs_by_step.values())):
        if result.tool_name == "video_understanding" and result.success and result.data:
            return result.data
    return {}


def latest_visual_output_ref(outputs_by_step: dict[str, ToolResult]) -> str | None:
    """Return the latest visual/video understanding output ref."""

    for result in reversed(list(outputs_by_step.values())):
        if result.tool_name in {"vision_understanding", "video_understanding"} and result.success:
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


def latest_memory_items(outputs_by_step: dict[str, ToolResult]) -> list[dict[str, Any]]:
    """Return retrieved memory items from the latest memory result."""

    for result in reversed(list(outputs_by_step.values())):
        if result.tool_name not in {"memory", "memory_retrieval"} or not result.success or not result.data:
            continue
        items = result.data.get("items")
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
        return [result.data]
    return []


def _merge_product_render_context(payload: dict[str, Any], product: dict[str, Any]) -> None:
    payload["product_ref"] = product.get("product_id") or product.get("product_ref")
    payload["product_id"] = product.get("product_id")
    payload["product_title"] = product.get("title") or product.get("product_title")
    payload["product_image_url"] = product.get("image_url")
    payload["image_url"] = product.get("image_url") or product.get("product_url") or product.get("url")
    if product.get("style_tags"):
        payload["style"] = ", ".join(product["style_tags"])
    if product.get("material"):
        payload["material"] = product["material"]


def _merge_visual_render_context(
    payload: dict[str, Any],
    visual: dict[str, Any],
    output_ref: str | None,
) -> None:
    summary = visual.get("summary")
    objects = visual.get("objects") if isinstance(visual.get("objects"), list) else []
    colors = visual.get("colors") if isinstance(visual.get("colors"), list) else []
    materials = visual.get("materials") if isinstance(visual.get("materials"), list) else []
    style_tags = visual.get("style_tags") if isinstance(visual.get("style_tags"), list) else []

    if summary:
        payload["visual_summary"] = summary
        payload["video_summary"] = summary
    if output_ref:
        payload["image_ref"] = output_ref
        payload["video_ref"] = output_ref
        payload.setdefault("image_url", output_ref)
    context_parts = [summary, "、".join(objects), "、".join(colors), "、".join(materials), "、".join(style_tags)]
    payload["memory_context"] = [part for part in context_parts if part]
    if style_tags and not payload.get("style"):
        payload["style"] = ", ".join(style_tags)
    if materials and not payload.get("material"):
        payload["material"] = ", ".join(materials)


def _merge_memory_render_context(payload: dict[str, Any], memory_items: list[dict[str, Any]]) -> None:
    summaries: list[str] = []
    for item in memory_items:
        summary = item.get("summary")
        if summary:
            summaries.append(summary)
        content = item.get("content") if isinstance(item.get("content"), dict) else {}
        if content and not payload.get("product_ref"):
            payload["product_ref"] = content.get("product_ref") or content.get("product_id") or content.get("item")
        if content and content.get("style") and not payload.get("style"):
            payload["style"] = content["style"]
    existing_context = payload.get("memory_context") or []
    payload["memory_context"] = [*existing_context, *summaries]
