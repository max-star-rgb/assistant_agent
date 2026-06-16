"""Prompt construction for text-first capabilities."""

from typing import Any

from multimodal_agent.schemas.requests import UserRequest
from multimodal_agent.schemas.tools import ToolResult
from multimodal_agent.schemas.capability_output import build_capability_output_contract
from multimodal_agent.services.chat_adapter import ChatRequest
from multimodal_agent.services.image_generation_adapter import ImageGenerationRequest


MAX_PROMPT_CHARS = 1200
MAX_CONTEXT_CHARS = 500


def build_direct_chat_request(
    request: UserRequest,
    memory_context: list[str] | None = None,
    system_instruction: str | None = None,
    max_prompt_chars: int = MAX_PROMPT_CHARS,
) -> ChatRequest:
    """Build a provider-neutral direct chat request."""

    return ChatRequest(
        user_id=request.user_id,
        session_id=request.session_id,
        user_query=_clip(request.text or "", max_prompt_chars),
        memory_context=_clip_list(memory_context or [], MAX_CONTEXT_CHARS),
        system_instruction=system_instruction or "You are a helpful text-first assistant.",
    )


def build_image_generation_request(
    request: UserRequest,
    outputs_by_step: dict[str, ToolResult],
    style: str | None = None,
    max_prompt_chars: int = MAX_PROMPT_CHARS,
) -> ImageGenerationRequest:
    """Build a provider-neutral image generation request from text and prior outputs."""

    product = _latest_product(outputs_by_step)
    visual_summary = _latest_visual_summary(outputs_by_step)
    memory_items = _latest_memory_items(outputs_by_step)
    memory_context = _memory_summaries(memory_items) or _request_memory_summaries(request)
    product_title = (
        product.get("title")
        or product.get("summary")
        or product.get("content", {}).get("item")
    )
    product_context = _compact_context(product)
    prompt = build_image_prompt_text(
        user_query=request.text or "",
        style=style or "日系海报",
        product_context=product_context,
        visual_summary=visual_summary,
        memory_context=memory_context,
        max_chars=max_prompt_chars,
    )
    return ImageGenerationRequest(
        prompt=prompt,
        style=style or "日系海报",
        product_id=product.get("product_id"),
        product_title=product_title,
        product_info=product,
        reference_image_ids=request.image_ids,
        memory_context=memory_context,
        user_id=request.user_id,
        session_id=request.session_id,
    )


def build_image_prompt_text(
    user_query: str,
    style: str | None = None,
    product_context: str | None = None,
    visual_summary: str | None = None,
    memory_context: list[str] | None = None,
    max_chars: int = MAX_PROMPT_CHARS,
) -> str:
    """Build a bounded prompt for image generation."""

    parts = [f"用户需求：{user_query.strip()}"]
    if style:
        parts.append(f"风格：{style}")
    if product_context:
        parts.append(f"商品上下文：{product_context}")
    if visual_summary:
        parts.append(f"视觉摘要：{visual_summary}")
    if memory_context:
        parts.append(f"记忆上下文：{'；'.join(_clip_list(memory_context, MAX_CONTEXT_CHARS))}")
    parts.append("输出要求：突出商品主体，构图清晰，适合营销展示。")
    return _clip("\n".join(part for part in parts if part), max_chars)


def build_text_capability_output(
    capability: str,
    status: str,
    output_ref: str | None = None,
    data: dict[str, Any] | None = None,
    errors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a stable public output contract without provider raw payloads."""

    payload = build_capability_output_contract(
        capability=capability,
        status=status,  # type: ignore[arg-type]
        output_ref=output_ref,
        data=data,
        errors=errors,
    ).model_dump(mode="json")
    if not payload.get("metadata"):
        payload.pop("metadata", None)
    return payload


def _latest_product(outputs_by_step: dict[str, ToolResult]) -> dict[str, Any]:
    for result in reversed(list(outputs_by_step.values())):
        if not result.data:
            continue
        if isinstance(result.data.get("items"), list) and result.data["items"]:
            return result.data["items"][0]
    return {}


def _latest_visual_summary(outputs_by_step: dict[str, ToolResult]) -> str | None:
    for result in reversed(list(outputs_by_step.values())):
        if result.tool_name in {"vision_understanding", "video_understanding"} and result.data:
            summary = result.data.get("summary")
            if isinstance(summary, str):
                return summary
    return None


def _latest_memory_items(outputs_by_step: dict[str, ToolResult]) -> list[dict[str, Any]]:
    for result in reversed(list(outputs_by_step.values())):
        if result.tool_name == "memory_retrieval" and result.data:
            items = result.data.get("items")
            if isinstance(items, list):
                return items
    return []


def _request_memory_summaries(request: UserRequest) -> list[str]:
    summaries = request.metadata.get("memory_context_summaries")
    if isinstance(summaries, list):
        return [summary for summary in summaries if isinstance(summary, str)]
    context_text = request.metadata.get("memory_context_text")
    if isinstance(context_text, str) and context_text.strip():
        return [context_text.strip()]
    return []


def _memory_summaries(items: list[dict[str, Any]]) -> list[str]:
    return [item["summary"] for item in items if isinstance(item.get("summary"), str)]


def _compact_context(value: dict[str, Any]) -> str | None:
    if not value:
        return None
    title = value.get("title") or value.get("summary") or value.get("content", {}).get("item")
    price = value.get("price")
    platform = value.get("platform")
    reason = value.get("reason")
    source = value.get("source")
    parts = [str(item) for item in (title, price, platform, reason, source) if item is not None]
    return " / ".join(parts) if parts else None


def _clip(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 1] + "…"


def _clip_list(values: list[str], max_chars: int) -> list[str]:
    clipped: list[str] = []
    total = 0
    for value in values:
        if total >= max_chars:
            break
        remaining = max_chars - total
        item = _clip(value, remaining)
        clipped.append(item)
        total += len(item)
    return clipped
