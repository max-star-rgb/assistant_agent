"""Single production parent graph for fast and planning execution modes."""

from __future__ import annotations

from functools import partial
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import RetryPolicy

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.memory import (
    MemoryBackend,
    memory_commit_degraded,
    memory_commit_node,
    memory_recall_degraded,
    memory_recall_node,
)
from assistant_agent.native_agent.state import AssistantRootInput, AssistantRootState
from assistant_agent.proactive_delivery import (
    ProactiveDeliveryIntent,
    ProactiveDeliveryStore,
    ProactiveDispatchState,
    ProactiveMessage,
)


class ProactiveDeliveryUnavailableError(RuntimeError):
    """A native graph delivery cannot reach its durable enqueue boundary."""


def build_assistant_root_graph(
    *,
    memory_backend: MemoryBackend,
    fast_agent: Any,
    planning_graph: Any,
    proactive_delivery_store: ProactiveDeliveryStore | None = None,
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
    builder.add_node("execution_router", execution_router_node)
    builder.add_node("fast_agent", fast_agent)
    builder.add_node("planning_graph", planning_graph)
    builder.add_node(
        "delivery_dispatch",
        partial(
            delivery_dispatch_node,
            store=proactive_delivery_store,
        ),
    )
    builder.add_node(
        "memory_commit",
        partial(memory_commit_node, backend=memory_backend),
        error_handler=memory_commit_degraded,
    )
    builder.add_edge(START, "memory_recall")
    builder.add_edge("memory_recall", "execution_router")
    builder.add_conditional_edges(
        "execution_router",
        route_execution_mode,
        {"fast": "fast_agent", "planning": "planning_graph"},
    )
    builder.add_conditional_edges(
        "fast_agent",
        route_after_execution,
        {"delivery_dispatch": "delivery_dispatch", "memory_commit": "memory_commit"},
    )
    builder.add_conditional_edges(
        "planning_graph",
        route_after_execution,
        {"delivery_dispatch": "delivery_dispatch", "memory_commit": "memory_commit"},
    )
    builder.add_edge("delivery_dispatch", "memory_commit")
    builder.add_edge("memory_commit", END)
    return builder.compile(name="AssistantRootGraph")


def execution_router_node(_state: AssistantRootState) -> dict[str, object]:
    """Expose one stable join point for normal and recovered recall execution."""

    return {}


def route_execution_mode(state: AssistantRootState) -> str:
    """Route exclusively from the trusted structured input channel."""

    return state["execution_mode"]


def route_after_execution(state: AssistantRootState) -> str:
    """Enter the native delivery boundary only for explicit state intents."""

    return "delivery_dispatch" if state.get("pending_deliveries") else "memory_commit"


def delivery_dispatch_node(
    state: AssistantRootState,
    runtime: Runtime[AssistantRunContext],
    *,
    store: ProactiveDeliveryStore | None,
) -> dict[str, object]:
    """Idempotently persist checkpoint-safe intents using native run identity."""

    pending = tuple(
        item
        if isinstance(item, ProactiveDeliveryIntent)
        else ProactiveDeliveryIntent.model_validate(item)
        for item in state.get("pending_deliveries", ())
    )
    if not pending:
        return {}
    if store is None:
        raise ProactiveDeliveryUnavailableError(
            "Pending proactive delivery requires a configured store."
        )
    execution = runtime.execution_info
    context = runtime.context
    if (
        execution is None
        or not execution.thread_id
        or not execution.run_id
        or context is None
    ):
        raise ProactiveDeliveryUnavailableError(
            "Proactive delivery requires native thread, run and owner identity."
        )
    records = [
        store.enqueue(
            ProactiveMessage(
                message_id=intent.message_id,
                user_id=context.user_id,
                session_id=execution.thread_id,
                kind=intent.kind,
                content=intent.content,
                delivery_mode=intent.delivery_mode,
                source_run_id=execution.run_id,
                source_trace_id=execution.run_id,
            )
        )
        for intent in pending
    ]
    all_skipped = all(record.status == "skipped_offline" for record in records)
    return {
        "pending_deliveries": (),
        "delivery_dispatch": ProactiveDispatchState(
            status="skipped" if all_skipped else "queued",
            message_ids=tuple(intent.message_id for intent in pending),
            issue_code="connection_offline" if all_skipped else None,
        ),
    }


__all__ = [
    "ProactiveDeliveryUnavailableError",
    "build_assistant_root_graph",
    "delivery_dispatch_node",
    "execution_router_node",
    "route_after_execution",
    "route_execution_mode",
]
