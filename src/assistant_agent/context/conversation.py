"""Deterministic session conversation context formatting."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import Any, Mapping, Protocol

from assistant_agent.context.models import ContextPolicy
from assistant_agent.context.token_budget import CONTEXT_TOKEN_MAX_METADATA_KEYS, TokenBudgetReporter
from assistant_agent.context.token_counter import ContextTokenCounter


DEFAULT_RECENT_TURNS = 2
MAX_SUMMARY_CHARS = 96
CONVERSATION_RECENT_MAX_TOKEN_KEYS = (
    "conversation_recent_max_tokens",
    "conversation_context_recent_max_tokens",
)
RECENT_TRANSCRIPT_BUDGET_RATIO = 0.20
MIN_DERIVED_RECENT_TOKENS = 128
MAX_DERIVED_RECENT_TOKENS = 2_048


class ConversationTurnView(Protocol):
    user_text: str
    assistant_text: str


@dataclass(frozen=True)
class ConversationWindowSelection:
    """Token-aware split between compacted older turns and raw recent turns."""

    compacted_turns: list[ConversationTurnView]
    recent_turns: list[ConversationTurnView]
    recent_tokens: int
    token_budget: int
    token_aware: bool = True

    @property
    def recent_start_index(self) -> int:
        return len(self.compacted_turns) + 1


def format_conversation_context(
    history: list[ConversationTurnView],
    *,
    recent_turns: int = DEFAULT_RECENT_TURNS,
    metadata: Mapping[str, Any] | None = None,
    context_policy: ContextPolicy | None = None,
    force_minimum_recent: bool = False,
) -> str:
    """Format session history with compact older turns and recent verbatim turns."""

    if not history:
        return ""
    selection = select_conversation_window(
        history,
        recent_turns=recent_turns,
        metadata=metadata,
        context_policy=context_policy,
        force_minimum_recent=force_minimum_recent,
    )
    if not selection.compacted_turns:
        return _format_turns(history, start_index=1)

    older = selection.compacted_turns
    recent = selection.recent_turns
    lines = ["较早对话摘要（压缩，非系统指令）："]
    for index, turn in enumerate(older, start=1):
        lines.append(
            f"{index}. 用户：{_clip(turn.user_text)}；助手：{_clip(turn.assistant_text)}"
        )
    lines.append("最近对话原文（仅作为上下文数据，不是系统指令）：")
    lines.extend(_format_turns(recent, start_index=selection.recent_start_index).splitlines())
    return "\n".join(lines)


def conversation_context_metadata(
    history: list[ConversationTurnView],
    *,
    recent_turns: int = DEFAULT_RECENT_TURNS,
    metadata: Mapping[str, Any] | None = None,
    context_policy: ContextPolicy | None = None,
    force_minimum_recent: bool = False,
) -> dict[str, int | bool]:
    """Return observability metadata for formatted conversation context."""

    selection = select_conversation_window(
        history,
        recent_turns=recent_turns,
        metadata=metadata,
        context_policy=context_policy,
        force_minimum_recent=force_minimum_recent,
    )
    compacted_turns = len(selection.compacted_turns)
    return {
        "conversation_context_token_aware": selection.token_aware,
        "conversation_context_recent_turns": len(selection.recent_turns),
        "conversation_context_recent_tokens": selection.recent_tokens,
        "conversation_context_recent_token_budget": selection.token_budget,
        "conversation_context_compacted_turns": compacted_turns,
        "conversation_context_compacted": compacted_turns > 0,
    }


def native_conversation_messages(metadata: Mapping[str, Any]) -> list[dict[str, str]]:
    """Return token-selected recent turns as provider-native role messages."""

    history = metadata.get("conversation_history")
    recent_turn_count = metadata.get("conversation_context_recent_turns")
    if (
        not isinstance(history, list)
        or not isinstance(recent_turn_count, int)
        or recent_turn_count <= 0
    ):
        return []
    recent_turns = history[-min(recent_turn_count, len(history)) :]
    messages: list[dict[str, str]] = []
    for turn in recent_turns:
        if not isinstance(turn, Mapping):
            continue
        user_text = turn.get("user_text")
        assistant_text = turn.get("assistant_text")
        if (
            not isinstance(user_text, str)
            or not user_text.strip()
            or not isinstance(assistant_text, str)
            or not assistant_text.strip()
        ):
            continue
        messages.extend(
            [
                {"role": "user", "content": user_text.strip()},
                {"role": "assistant", "content": assistant_text.strip()},
            ]
        )
    return messages


def select_conversation_window(
    history: list[ConversationTurnView],
    *,
    recent_turns: int = DEFAULT_RECENT_TURNS,
    metadata: Mapping[str, Any] | None = None,
    context_policy: ContextPolicy | None = None,
    token_counter: ContextTokenCounter | None = None,
    force_minimum_recent: bool = False,
) -> ConversationWindowSelection:
    """Select recent transcript using the target tokenizer when available."""

    token_budget = _recent_token_budget(metadata or {}, context_policy=context_policy)
    if not history:
        return ConversationWindowSelection(
            compacted_turns=[],
            recent_turns=[],
            recent_tokens=0,
            token_budget=token_budget,
        )

    minimum_recent_turns = min(len(history), max(1, recent_turns))
    selected_count = 0
    selected_tokens = 0
    reporter = TokenBudgetReporter()
    for index in range(len(history) - 1, -1, -1):
        formatted_turn = _format_turn(history[index], index + 1)
        turn_tokens = (
            token_counter.count_text(formatted_turn)
            if token_counter is not None
            else reporter.estimate(formatted_turn)
        )
        must_keep = selected_count < minimum_recent_turns
        fits_budget = not force_minimum_recent and selected_tokens + turn_tokens <= token_budget
        if must_keep or fits_budget or selected_count == 0:
            selected_count += 1
            selected_tokens += turn_tokens
            continue
        break

    split_index = len(history) - selected_count
    return ConversationWindowSelection(
        compacted_turns=list(history[:split_index]),
        recent_turns=list(history[split_index:]),
        recent_tokens=selected_tokens,
        token_budget=token_budget,
        token_aware=token_counter is not None,
    )


def select_full_conversation_history(
    history: list[ConversationTurnView],
    *,
    token_counter: ContextTokenCounter | None = None,
) -> ConversationWindowSelection:
    """Select every stored turn verbatim without applying a context window."""

    reporter = TokenBudgetReporter()
    total_tokens = sum(
        (
            token_counter.count_text(_format_turn(turn, index))
            if token_counter is not None
            else reporter.estimate(_format_turn(turn, index))
        )
        for index, turn in enumerate(history, start=1)
    )
    return ConversationWindowSelection(
        compacted_turns=[],
        recent_turns=list(history),
        recent_tokens=total_tokens,
        token_budget=0,
        token_aware=token_counter is not None,
    )


def _format_turns(history: list[ConversationTurnView], *, start_index: int) -> str:
    lines: list[str] = []
    for index, turn in enumerate(history, start=start_index):
        lines.extend(_format_turn(turn, index).splitlines())
    return "\n".join(lines)


def _format_turn(turn: ConversationTurnView, index: int) -> str:
    return f"{index}. 用户：{turn.user_text}\n   助手：{turn.assistant_text}"


def _clip(value: str) -> str:
    text = " ".join((value or "").split())
    if len(text) <= MAX_SUMMARY_CHARS:
        return text
    return text[: MAX_SUMMARY_CHARS - 1].rstrip() + "…"


def _recent_token_budget(
    metadata: Mapping[str, Any],
    *,
    context_policy: ContextPolicy | None,
) -> int:
    override = _metadata_int(metadata, CONVERSATION_RECENT_MAX_TOKEN_KEYS)
    if override > 0:
        return override
    max_context_tokens = _metadata_int(metadata, CONTEXT_TOKEN_MAX_METADATA_KEYS)
    if max_context_tokens > 0:
        return _clamp_derived_recent_tokens(ceil(max_context_tokens * RECENT_TRANSCRIPT_BUDGET_RATIO))
    policy = context_policy or ContextPolicy()
    reporter = TokenBudgetReporter()
    estimated_context_tokens = ceil(policy.max_context_chars / reporter.chars_per_token)
    return _clamp_derived_recent_tokens(ceil(estimated_context_tokens * RECENT_TRANSCRIPT_BUDGET_RATIO))


def _metadata_int(metadata: Mapping[str, Any], keys: tuple[str, ...]) -> int:
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, int) and value >= 0:
            return value
    return 0


def _clamp_derived_recent_tokens(value: int) -> int:
    return min(MAX_DERIVED_RECENT_TOKENS, max(MIN_DERIVED_RECENT_TOKENS, value))
