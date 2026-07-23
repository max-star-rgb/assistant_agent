"""Token-aware memory context building."""

from __future__ import annotations

from datetime import datetime, timezone
from math import ceil
from typing import Literal

from pydantic import BaseModel, Field

from assistant_agent.schemas.memory import MemoryItem


MemoryLayer = Literal["session", "semantic", "episodic", "artifact", "procedural"]
MEMORY_CONTEXT_RETRIEVAL_VERSION = "memory_context_builder_v1"


class MemoryContextBlock(BaseModel):
    """A prompt-safe grouped view of injected memories."""

    layer: MemoryLayer
    title: str
    items: list[MemoryItem] = Field(default_factory=list)


class MemoryContextPack(BaseModel):
    """Token-aware memory context selected for one model run."""

    items: list[MemoryItem] = Field(default_factory=list)
    blocks: list[MemoryContextBlock] = Field(default_factory=list)
    rendered_context: str = ""
    total_tokens: int = Field(default=0, ge=0)
    budget_tokens: int = Field(default=0, ge=0)
    omitted_count: int = Field(default=0, ge=0)
    rejected_reasons: list[str] = Field(default_factory=list)
    retrieval_version: str = MEMORY_CONTEXT_RETRIEVAL_VERSION


class MemoryContextBuilder:
    """Select and render prompt-safe memory context within character/token budgets."""

    def __init__(self, *, chars_per_token: float = 4.0) -> None:
        self.chars_per_token = max(chars_per_token, 1.0)

    def build(
        self,
        items: list[MemoryItem],
        *,
        budget_tokens: int | None = None,
        max_chars: int | None = None,
    ) -> MemoryContextPack:
        """Return the subset of memory items that fits the configured context budget."""

        if not items:
            return MemoryContextPack(budget_tokens=_positive_int(budget_tokens))

        safe_items, rejected_reasons = self._filter_injectable(items)
        if not safe_items:
            return MemoryContextPack(
                budget_tokens=_positive_int(budget_tokens),
                omitted_count=len(items),
                rejected_reasons=rejected_reasons,
            )

        selected, lines, budget_omissions = self._select_lines(
            safe_items,
            budget_tokens=_positive_int(budget_tokens),
            max_chars=_positive_int(max_chars),
        )
        rendered = "\n".join(lines) if selected else ""
        total_tokens = self.estimate_tokens(rendered)
        omitted_count = len(items) - len(selected)
        return MemoryContextPack(
            items=selected,
            blocks=group_by_layer(selected),
            rendered_context=rendered,
            total_tokens=total_tokens,
            budget_tokens=_positive_int(budget_tokens),
            omitted_count=omitted_count,
            rejected_reasons=[*rejected_reasons, *budget_omissions],
        )

    def estimate_tokens(self, value: str) -> int:
        """Estimate tokens deterministically without provider calls or tokenizer dependencies."""

        text = value or ""
        if not text:
            return 0
        cjk_chars = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
        other_chars = len(text) - cjk_chars
        return cjk_chars + ceil(other_chars / self.chars_per_token)

    def _filter_injectable(self, items: list[MemoryItem]) -> tuple[list[MemoryItem], list[str]]:
        now = datetime.now(timezone.utc)
        safe_items: list[MemoryItem] = []
        rejected_reasons: list[str] = []
        for item in items:
            if item.sensitivity == "sensitive":
                rejected_reasons.append(f"{item.memory_id}:sensitive_memory_not_injected")
                continue
            if item.expires_at is not None:
                item_now = now.astimezone(item.expires_at.tzinfo or timezone.utc)
                if item.expires_at < item_now:
                    rejected_reasons.append(f"{item.memory_id}:expired_memory_not_injected")
                    continue
            safe_items.append(item)
        return safe_items, rejected_reasons

    def _select_lines(
        self,
        items: list[MemoryItem],
        *,
        budget_tokens: int,
        max_chars: int,
    ) -> tuple[list[MemoryItem], list[str], list[str]]:
        lines: list[str] = []
        selected: list[MemoryItem] = []
        omitted_reasons: list[str] = []

        for block in group_by_layer(items):
            for item in block.items:
                additions = [_format_item_line(item)]
                candidate_lines = [*lines, *additions]
                candidate_text = "\n".join(candidate_lines)
                if max_chars > 0 and len(candidate_text) > max_chars:
                    omitted_reasons.append(f"{item.memory_id}:memory_context_char_budget_exceeded")
                    continue
                if budget_tokens > 0 and self.estimate_tokens(candidate_text) > budget_tokens:
                    omitted_reasons.append(f"{item.memory_id}:memory_context_token_budget_exceeded")
                    continue
                lines.extend(additions)
                selected.append(item)

        return selected, lines, omitted_reasons


def format_layered_memory_context(
    blocks: list[MemoryContextBlock],
    *,
    max_chars: int = 500,
    budget_tokens: int | None = None,
) -> str:
    """Format already-grouped blocks through the token-aware builder."""

    items = [item for block in blocks for item in block.items]
    return MemoryContextBuilder().build(
        items,
        max_chars=max_chars,
        budget_tokens=budget_tokens,
    ).rendered_context


def group_by_layer(items: list[MemoryItem]) -> list[MemoryContextBlock]:
    """Group memory items by stable prompt-context layer."""

    grouped: dict[MemoryLayer, list[MemoryItem]] = {
        "semantic": [],
        "session": [],
        "episodic": [],
        "artifact": [],
        "procedural": [],
    }
    for item in items:
        grouped[_layer_for(item)].append(item)

    blocks: list[MemoryContextBlock] = []
    for layer, title in _LAYER_TITLES:
        layer_items = grouped[layer]
        if layer_items:
            blocks.append(MemoryContextBlock(layer=layer, title=title, items=layer_items))
    return blocks


def _format_item_line(item: MemoryItem) -> str:
    return item.summary.strip()


def _layer_for(item: MemoryItem) -> MemoryLayer:
    if item.memory_type == "preference":
        return "semantic"
    if item.memory_type == "conversation":
        return "session"
    if item.memory_type == "task":
        return "episodic"
    return "artifact"


def _positive_int(value: int | None) -> int:
    return value if isinstance(value, int) and value > 0 else 0


_LAYER_TITLES: list[tuple[MemoryLayer, str]] = [
    ("semantic", "偏好/事实记忆："),
    ("session", "长期化对话："),
    ("episodic", "任务/经历记忆："),
    ("artifact", "产物/对象引用："),
    ("procedural", "过程/规则记忆："),
]
