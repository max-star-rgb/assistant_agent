"""Deterministic prompt ToolSpec recall for assistant context rendering."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from multimodal_agent.schemas.context import ToolCatalogSummary
from multimodal_agent.schemas.requests import UserRequest
from multimodal_agent.schemas.tools import ToolSpec


@dataclass(frozen=True)
class ToolCatalogSelection:
    """Tool specs selected for prompt rendering plus a traceable summary."""

    prompt_tool_specs: list[ToolSpec]
    summary: ToolCatalogSummary


def select_prompt_tool_specs(request: UserRequest, tool_specs: list[ToolSpec]) -> ToolCatalogSelection:
    """Select a small prompt-facing ToolSpec set, falling back to all tools when unsure."""

    total = len(tool_specs)
    if not tool_specs:
        return ToolCatalogSelection(
            prompt_tool_specs=[],
            summary=ToolCatalogSummary(selection_reasons=["no_tools_available"]),
        )

    selected_names: list[str] = []
    reasons: list[str] = []
    text = _normalized_text(request.text)

    if _has_price_compare_intent(text):
        _add(selected_names, "product_search")
        _add(selected_names, "price_compare")
        reasons.append("price_compare_keyword: compare/lowest-price/discount request")
    elif _has_product_search_intent(text):
        _add(selected_names, "product_search")
        reasons.append("product_search_keyword: product/search/buy/recommend request")

    if request.image_ids:
        _add(selected_names, "vision_understanding")
        reasons.append("image_ids_present: image understanding tool is relevant")
    elif _has_image_understanding_intent(text):
        _add(selected_names, "vision_understanding")
        reasons.append("image_understanding_keyword: image/OCR/recognition request")

    if request.video_ids:
        _add(selected_names, "video_understanding")
        reasons.append("video_ids_present: video understanding tool is relevant")
    elif _has_video_understanding_intent(text):
        _add(selected_names, "video_understanding")
        reasons.append("video_understanding_keyword: video analysis request")

    if _has_image_generation_intent(text):
        _add(selected_names, "image_generation")
        reasons.append("image_generation_keyword: draw/generate-image request")

    if _has_render_intent(text):
        _add(selected_names, "render_3d")
        reasons.append("render_3d_keyword: explicit 3D/render/modeling request")

    if _has_memory_intent(text):
        for name in ("memory_retrieval", "memory_save", "memory"):
            _add(selected_names, name)
        reasons.append("memory_keyword: remember/preference/history request")

    available_by_name = {spec.name: spec for spec in tool_specs}
    prompt_specs = [available_by_name[name] for name in selected_names if name in available_by_name]
    if not prompt_specs:
        return _fallback(tool_specs, reason="fallback_full_tool_list: no reliable tool match")

    prompt_names = [spec.name for spec in prompt_specs]
    return ToolCatalogSelection(
        prompt_tool_specs=prompt_specs,
        summary=ToolCatalogSummary(
            total_tool_count=total,
            prompt_tool_count=len(prompt_specs),
            filtered_tool_count=max(total - len(prompt_specs), 0),
            selected_tool_names=prompt_names,
            selection_reasons=reasons or ["matched_tool_names"],
            fallback_used=False,
        ),
    )


def _fallback(tool_specs: list[ToolSpec], *, reason: str) -> ToolCatalogSelection:
    tool_names = [spec.name for spec in tool_specs]
    return ToolCatalogSelection(
        prompt_tool_specs=list(tool_specs),
        summary=ToolCatalogSummary(
            total_tool_count=len(tool_specs),
            prompt_tool_count=len(tool_specs),
            filtered_tool_count=0,
            selected_tool_names=tool_names,
            selection_reasons=[reason],
            fallback_used=True,
        ),
    )


def _normalized_text(text: str | None) -> str:
    return (text or "").strip().lower()


def _has_price_compare_intent(text: str) -> bool:
    return _contains_any(
        text,
        (
            "比价",
            "最低价",
            "最低价格",
            "最便宜",
            "哪里便宜",
            "优惠",
            "折扣",
            "券",
            "compare price",
            "price compare",
            "lowest price",
            "cheapest",
            "discount",
            "deal",
        ),
    )


def _has_product_search_intent(text: str) -> bool:
    if _contains_any(
        text,
        (
            "商品",
            "产品",
            "购买",
            "买",
            "下单",
            "电商",
            "淘宝",
            "京东",
            "相似款",
            "同款",
            "商品链接",
            "搜索",
            "搜一下",
            "推荐",
            "product",
            "shopping",
            "buy",
            "purchase",
            "recommend",
            "search",
        ),
    ):
        return True
    return "找" in text and _contains_any(text, _PRODUCT_HINTS)


def _has_image_understanding_intent(text: str) -> bool:
    if _has_image_generation_intent(text) or _has_render_intent(text):
        return False
    return _contains_any(
        text,
        (
            "识图",
            "图片理解",
            "看图",
            "图里",
            "图中",
            "图片里",
            "图片中",
            "这张图",
            "这张图片",
            "描述图片",
            "分析图片",
            "识别图片",
            "画面",
            "文字识别",
            "ocr",
            "image understanding",
            "describe image",
            "what is in this image",
        ),
    )


def _has_video_understanding_intent(text: str) -> bool:
    return _contains_any(
        text,
        (
            "视频",
            "录像",
            "片段",
            "视频里",
            "视频中",
            "发生了什么",
            "video",
            "clip",
        ),
    )


def _has_image_generation_intent(text: str) -> bool:
    return _contains_any(
        text,
        (
            "画图",
            "画一张",
            "生成图片",
            "生成一张",
            "生成图",
            "图片生成",
            "出图",
            "做图",
            "绘制",
            "电商主图",
            "海报",
            "draw",
            "generate an image",
            "generate image",
            "create an image",
        ),
    )


def _has_render_intent(text: str) -> bool:
    if _contains_any(text, ("3d", "三维", "渲染", "建模", "场景预览", "展示空间", "render")):
        return True
    if "模型" in text and _contains_any(text, ("3d", "三维", "建模", "渲染", "商品", "展示")):
        return True
    return "场景" in text and _contains_any(text, ("创建", "生成", "渲染", "建模", "预览", "展示", "放进", "放入", "放到"))


def _has_memory_intent(text: str) -> bool:
    return _contains_any(
        text,
        (
            "记住",
            "记下来",
            "帮我记",
            "保存偏好",
            "我的偏好",
            "按我的偏好",
            "根据我的偏好",
            "上次",
            "之前",
            "以前聊过",
            "历史对话",
            "历史记录",
            "聊天记录",
            "记忆",
            "记得我",
            "继续上次",
            "remember",
            "my preference",
            "saved preference",
            "last time",
            "previous conversation",
            "chat history",
            "conversation history",
        ),
    )


def _contains_any(text: str, needles: Iterable[str]) -> bool:
    return any(needle.lower() in text for needle in needles)


def _add(names: list[str], name: str) -> None:
    if name not in names:
        names.append(name)


_PRODUCT_HINTS = (
    "耳机",
    "鞋",
    "包",
    "衣服",
    "手机",
    "电脑",
    "椅",
    "桌",
    "灯",
    "相似款",
    "同款",
    "商品",
    "产品",
    "价格",
)
