"""Single production parent graph for fast and planning execution modes."""

from __future__ import annotations

from functools import partial
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.memory import (
    MemoryBackend,
    memory_commit_node,
    memory_recall_degraded,
    memory_recall_node,
)
from assistant_agent.native_agent.state import AssistantRootInput, AssistantRootState


def build_assistant_root_graph(
    *,
    memory_backend: MemoryBackend,
    fast_agent: Any,
    planning_graph: Any,
):
    """Compose the only top-level runtime without binding saver or Store."""

    if getattr(fast_agent, "name", None) != "AssistantFastAgent":
        raise ValueError("fast branch must be AssistantFastAgent")
    if getattr(planning_graph, "name", None) != "AssistantPlanningGraph":
        raise ValueError("planning branch must be AssistantPlanningGraph")

    builder = StateGraph(
        AssistantRootState,
        input_schema=AssistantRootInput,
        context_schema=AssistantRunContext,
    )
    builder.add_node(
        "memory_recall",
        partial(memory_recall_node, backend=memory_backend),
        retry_policy=RetryPolicy(
            initial_interval=0,
            backoff_factor=0,
            max_attempts=3,
            jitter=False,
        ),
        error_handler=memory_recall_degraded,
    )
    builder.add_node("fast_agent", fast_agent)
    builder.add_node("planning_graph", planning_graph)
    builder.add_node(
        "memory_commit",
        partial(memory_commit_node, backend=memory_backend),
    )
    builder.add_edge(START, "memory_recall")
    builder.add_conditional_edges(
        "memory_recall",
        route_execution_mode,
        {"fast": "fast_agent", "planning": "planning_graph"},
    )
    builder.add_conditional_edges(
        "__error_handler__memory_recall",
        route_execution_mode,
        {"fast": "fast_agent", "planning": "planning_graph"},
    )
    builder.add_edge("fast_agent", "memory_commit")
    builder.add_edge("planning_graph", "memory_commit")
    builder.add_edge("memory_commit", END)
    return builder.compile(name="AssistantRootGraph")


def route_execution_mode(state: AssistantRootState) -> str:
    """Route exclusively from the trusted structured input channel."""

    return state["execution_mode"]


__all__ = ["build_assistant_root_graph", "route_execution_mode"]
