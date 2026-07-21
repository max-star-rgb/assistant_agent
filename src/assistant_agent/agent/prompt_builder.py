"""Prompt construction for text-first capabilities."""

from typing import Any

from assistant_agent.agent.system_prompt_policy import SystemPromptProfile, render_system_instruction
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.chat_adapter import ChatRequest
from assistant_agent.services.image_generation_adapter import ImageGenerationRequest
from assistant_agent.services.prompt_builder import (
    MAX_CONTEXT_CHARS,
    MAX_PROMPT_CHARS,
    build_image_prompt_text,
    build_text_capability_output,  # noqa: F401 - re-exported for existing agent nodes.
    clip_list,
    clip_text,
)
from assistant_agent.schemas.tool_ids import (
    IMAGE_UNDERSTANDING_TOOL_NAME,
    MEMORY_RETRIEVAL_TOOL_NAME,
    SHOPPING_SEARCH_TOOL_NAME,
)


def build_direct_chat_request(
    request: UserRequest,
    memory_context: list[str] | None = None,
    system_instruction: str | None = None,
    max_prompt_chars: int = MAX_PROMPT_CHARS,
) -> ChatRequest:
    """Build a provider-neutral direct chat request."""

    contexts = list(memory_context or [])
    conversation_context = request.metadata.get("conversation_context_text")
    if isinstance(conversation_context, str) and conversation_context.strip():
        contexts.append("多轮对话历史：\n" + conversation_context.strip())
    return ChatRequest(
        user_id=request.user_id,
        session_id=request.session_id,
        user_query=clip_text(request.text or "", max_prompt_chars),
        memory_context=clip_list(contexts, MAX_CONTEXT_CHARS),
        system_instruction=system_instruction or render_system_instruction(SystemPromptProfile.TEXT_DEFAULT),
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


def _latest_product(outputs_by_step: dict[str, ToolResult]) -> dict[str, Any]:
    for result in reversed(list(outputs_by_step.values())):
        if not result.data:
            continue
        if result.tool_name == SHOPPING_SEARCH_TOOL_NAME:
            best_offer = result.data.get("best_offer")
            if isinstance(best_offer, dict) and best_offer:
                return best_offer
            search = result.data.get("search")
            if isinstance(search, dict) and isinstance(search.get("items"), list) and search["items"]:
                return search["items"][0]
        if isinstance(result.data.get("items"), list) and result.data["items"]:
            return result.data["items"][0]
    return {}


def _latest_visual_summary(outputs_by_step: dict[str, ToolResult]) -> str | None:
    for result in reversed(list(outputs_by_step.values())):
        if result.tool_name == IMAGE_UNDERSTANDING_TOOL_NAME and result.data:
            summary = result.data.get("summary")
            if isinstance(summary, str):
                return summary
    return None


def _latest_memory_items(outputs_by_step: dict[str, ToolResult]) -> list[dict[str, Any]]:
    for result in reversed(list(outputs_by_step.values())):
        if result.tool_name == MEMORY_RETRIEVAL_TOOL_NAME and result.data:
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
    return clip_text(value, max_chars)


def _clip_list(values: list[str], max_chars: int) -> list[str]:
    return clip_list(values, max_chars)
