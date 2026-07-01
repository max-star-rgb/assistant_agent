"""Runtime dependency binding for LangGraph nodes.

LangGraph checkpoints serialize graph state. Agent dependencies such as tool
executors, model adapters, stores, and managers are runtime objects and must not
be persisted in that state. This module binds those dependencies around node
execution and strips them before the node result returns to LangGraph.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar, cast

from assistant_agent.agent.intent import IntentDetector
from assistant_agent.agent.router import ToolRouter
from assistant_agent.agent.tool_executor import ToolExecutor
from assistant_agent.memory.manager import MemoryManager
from assistant_agent.services.chat_adapter import ChatAdapter
from assistant_agent.services.context.compactor import ContextCompactor
from assistant_agent.services.trace_store import TraceStore, trace_graph_node


GraphState = dict[str, Any]
GraphStateT = TypeVar("GraphStateT", bound=dict[str, Any])


RUNTIME_STATE_KEYS = frozenset(
    {
        "intent_detector",
        "router",
        "tool_executor",
        "chat_adapter",
        "context_compactor",
        "memory_manager",
        "trace_store",
        "current_node_name",
    }
)


@dataclass(frozen=True)
class GraphRuntimeContext:
    """Non-checkpointable dependencies used by graph nodes."""

    tool_executor: ToolExecutor
    chat_adapter: ChatAdapter
    memory_manager: MemoryManager
    context_compactor: ContextCompactor | None = None
    intent_detector: IntentDetector | None = None
    router: ToolRouter | None = None
    trace_store: TraceStore | None = None


def bind_runtime_node(
    node_name: str,
    node_func: Callable[[GraphStateT], GraphStateT],
    runtime_context: GraphRuntimeContext | None = None,
    *,
    trace: bool = True,
) -> Callable[[GraphStateT], GraphStateT]:
    """Return a node that injects runtime objects only during execution."""

    executable = trace_graph_node(node_name, node_func) if trace else node_func
    if runtime_context is None:
        return executable

    def wrapped(graph_state: GraphStateT) -> GraphStateT:
        enriched_state = _with_runtime_context(graph_state, runtime_context)
        result = executable(enriched_state)
        return cast(GraphStateT, strip_runtime_context(result))

    return wrapped


def strip_runtime_context(graph_state: GraphState) -> GraphState:
    """Remove runtime-only keys from a graph state copy."""

    clean_state = dict(graph_state)
    for key in RUNTIME_STATE_KEYS:
        clean_state.pop(key, None)
    return clean_state


def _with_runtime_context(graph_state: GraphStateT, runtime_context: GraphRuntimeContext) -> GraphStateT:
    enriched_state = dict(graph_state)
    if runtime_context.intent_detector is not None:
        enriched_state["intent_detector"] = runtime_context.intent_detector
    if runtime_context.router is not None:
        enriched_state["router"] = runtime_context.router
    enriched_state["tool_executor"] = runtime_context.tool_executor
    enriched_state["chat_adapter"] = runtime_context.chat_adapter
    if runtime_context.context_compactor is not None:
        enriched_state["context_compactor"] = runtime_context.context_compactor
    enriched_state["memory_manager"] = runtime_context.memory_manager
    if runtime_context.trace_store is not None:
        enriched_state["trace_store"] = runtime_context.trace_store
    return cast(GraphStateT, enriched_state)
