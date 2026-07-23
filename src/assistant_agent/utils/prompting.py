"""Shared prompt helpers that are safe for tools, providers, and services."""

from assistant_agent.schemas.generation import ImageGenerationRequest


MAX_PROMPT_CHARS = 1200
MAX_CONTEXT_CHARS = 500


def build_image_prompt(input: ImageGenerationRequest) -> str:
    """Build a deterministic prompt from product information and style."""

    product = input.product_title or input.product_info.get("title") or input.product_id
    style = input.style or ("日系海报" if product else None)
    product_context = product
    if input.product_info:
        product_context = product_context or input.product_info.get("summary")
    if not product and not input.prompt:
        raise ValueError("缺少生成 prompt 或商品信息，无法生成图片")

    return build_image_prompt_text(
        user_query=input.prompt or "",
        style=style,
        product_context=product_context,
        memory_context=input.memory_context,
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
        parts.append(f"记忆上下文：{'；'.join(clip_list(memory_context, MAX_CONTEXT_CHARS))}")
    parts.append("输出要求：突出商品主体，构图清晰，适合营销展示。")
    return clip_text("\n".join(part for part in parts if part), max_chars)


def clip_text(value: str, max_chars: int) -> str:
    """Clip text to a bounded size for prompts and public summaries."""

    if len(value) <= max_chars:
        return value
    return value[: max_chars - 1] + "…"


def clip_list(values: list[str], max_chars: int) -> list[str]:
    """Clip a list while keeping the combined text bounded."""

    clipped: list[str] = []
    total = 0
    for value in values:
        if total >= max_chars:
            break
        remaining = max_chars - total
        item = clip_text(value, remaining)
        clipped.append(item)
        total += len(item)
    return clipped
