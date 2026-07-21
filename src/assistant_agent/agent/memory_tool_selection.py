"""LLM-first memory-tool selection audit for the assistant loop.

This module does not read, write, or classify memory content. It records the
LLM's selected memory tool and declared source intent for trace/debug use.
Keyword and vector hooks are intentionally inactive in the current path.
"""

from __future__ import annotations

from typing import Any

from assistant_agent.schemas.assistant_decision import AssistantDecision
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tool_ids import MEMORY_RETRIEVAL_TOOL_NAME, MEMORY_SAVE_TOOL_NAME


STRATEGY_NAME = "llm_first_hybrid"
MEMORY_TOOL_NAMES = {MEMORY_RETRIEVAL_TOOL_NAME, MEMORY_SAVE_TOOL_NAME}


def build_memory_tool_selection_audit(
    *,
    request: UserRequest,
    decision: AssistantDecision,
    state: Any,
    iteration: int,
    max_iterations: int,
    is_mock: bool,
) -> dict[str, Any]:
    """Build a prompt-safe audit record for memory tool selection signals."""

    selected_tool = _selected_memory_tool(decision)
    tool_input = decision.tool_input if isinstance(decision.tool_input, dict) else {}
    source_intent = tool_input.get("source_intent") if _is_memory_save_selection(selected_tool, tool_input) else None
    action = "llm_selected_memory_tool" if selected_tool else "audit_only"
    return {
        "strategy": STRATEGY_NAME,
        "iteration": iteration + 1,
        "max_iterations": max_iterations,
        "llm_decision_type": decision.type,
        "llm_tool_name": decision.tool_name,
        "selected_memory_tool": selected_tool,
        "source_intent": source_intent,
        "source_intent_present": isinstance(source_intent, str) and bool(source_intent.strip()),
        "source_detail_present": _source_detail_present(tool_input),
        "keyword_signals": [],
        "vector_shadow_signal": {"source": "disabled", "hit_count": 0, "real_vector_model": False},
        "missed_signals": [],
        "candidate_mode": "audit_only",
        "auto_write": False,
        "prior_memory_tool_calls": _prior_memory_tool_calls(state),
        "action": action,
        "llm_first_only": True,
    }


def record_memory_tool_selection_audit(request: UserRequest, audit: dict[str, Any]) -> None:
    """Store the latest memory selection audit record and bounded history."""

    request.metadata["memory_tool_selection"] = audit
    history = request.metadata.setdefault("memory_tool_selection_history", [])
    if isinstance(history, list):
        history.append(audit)
        del history[:-10]


def _selected_memory_tool(decision: AssistantDecision) -> str | None:
    if decision.type != "tool_call" or not decision.tool_name:
        return None
    return decision.tool_name if decision.tool_name in MEMORY_TOOL_NAMES else None


def _is_memory_save_selection(selected_tool: str | None, tool_input: dict[str, Any]) -> bool:
    return selected_tool == MEMORY_SAVE_TOOL_NAME


def _source_detail_present(tool_input: dict[str, Any]) -> bool:
    return all(_non_empty_text(tool_input.get(key)) for key in ("source_reason", "future_use", "evidence"))


def _prior_memory_tool_calls(state: Any) -> list[str]:
    tool_calls = getattr(state, "tool_calls", [])
    names: list[str] = []
    for call in tool_calls:
        tool_name = getattr(call, "tool_name", None)
        if isinstance(tool_name, str) and tool_name in MEMORY_TOOL_NAMES:
            names.append(tool_name)
    return names


def _non_empty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())
