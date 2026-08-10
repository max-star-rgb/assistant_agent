"""Shared contracts for foreground and durable Plan-and-Execute models."""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator, Field


def _normalize_display_title(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.split())
    if not normalized:
        raise ValueError("display_title must contain visible text")
    return normalized


PlanDisplayTitle = Annotated[
    str | None,
    Field(
        min_length=1,
        max_length=160,
        description=(
            "面向用户展示的简短当前进度，使用自然语言描述正在完成的具体工作；"
            "不得包含内部 ID、prompt 或敏感参数。"
        ),
    ),
    AfterValidator(_normalize_display_title),
]
