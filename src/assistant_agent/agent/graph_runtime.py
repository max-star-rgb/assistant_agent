"""Runtime dependency binding for LangGraph nodes.

LangGraph checkpoints serialize graph state. Agent dependencies such as tool
executors, model adapters, stores, and managers are runtime objects and must not
be persisted in that state. This module binds those dependencies around node
execution and strips them before the node result returns to LangGraph.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar, cast

from assistant_agent.agent.cancellation import raise_if_cancelled
from assistant_agent.agent.intent import IntentDetector
from assistant_agent.agent.router import ToolRouter
from assistant_agent.agent.tool_executor import ToolExecutor
from assistant_agent.memory.manager import MemoryManager
from assistant_agent.services.chat_adapter import ChatAdapter
from assistant_agent.services.context.compactor import ContextCompactor
from assistant_agent.services.event_sink import EventSink
from assistant_agent.services.session_memory_context import SessionMemoryContextStore
from assistant_agent.services.trace_store import TraceStore, trace_graph_node


GraphState = dict[str, Any]
GraphStateT = TypeVar("GraphStateT", bound=dict[str, Any])


RUNTIME_STATE_KEYS = frozenset(
    {
        "intent_detector",
        "router",
        "tool_executor",
        "chat_adapter",
        "chat_turn",
        "context_compactor",
        "context_projector",
        "memory_manager",
        "session_memory_context_store",
        "trace_store",
        "event_sink",
        "current_node_name",
        "cancel_token",
    }
)


@dataclass(frozen=True)
class GraphRuntimeContext:
    """Non-checkpointable dependencies used by graph nodes."""

    tool_executor: ToolExecutor
    chat_adapter: ChatAdapter
    memory_manager: MemoryManager
    session_memory_context_store: SessionMemoryContextStore
    chat_turn: Callable[[Any], Any] | None = None
    context_compactor: ContextCompactor | None = None
    context_projector: Callable[[Any], None] | None = None
    intent_detector: IntentDetector | None = None
    router: ToolRouter | None = None
    trace_store: TraceStore | None = None
    event_sink: EventSink | None = None
    cancel_token: Any | None = None


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
        raise_if_cancelled(runtime_context.cancel_token, phase="before_node", node_name=node_name)
        enriched_state = _with_runtime_context(graph_state, runtime_context)
        result = executable(enriched_state)
        raise_if_cancelled(
            runtime_context.cancel_token,
            phase="after_node",
            node_name=node_name,
            state=result.get("state") if isinstance(result, dict) else None,
        )
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
    if runtime_context.chat_turn is not None:
        enriched_state["chat_turn"] = runtime_context.chat_turn
    if runtime_context.context_compactor is not None:
        enriched_state["context_compactor"] = runtime_context.context_compactor
    if runtime_context.context_projector is not None:
        enriched_state["context_projector"] = runtime_context.context_projector
    enriched_state["memory_manager"] = runtime_context.memory_manager
    enriched_state["session_memory_context_store"] = (
        runtime_context.session_memory_context_store
    )
    if runtime_context.trace_store is not None:
        enriched_state["trace_store"] = runtime_context.trace_store
    if runtime_context.event_sink is not None:
        enriched_state["event_sink"] = runtime_context.event_sink
    if runtime_context.cancel_token is not None:
        enriched_state["cancel_token"] = runtime_context.cancel_token
    return cast(GraphStateT, enriched_state)
