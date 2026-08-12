"""Runtime dependency binding for LangGraph nodes.

LangGraph checkpoints serialize graph state. Agent dependencies such as tool
executors, model adapters, stores, and managers are runtime objects and must not
be persisted in that state. This module binds those dependencies around node
execution and strips them before the node result returns to LangGraph.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar, cast

from langgraph.runtime import Runtime

from assistant_agent.runtime.cancellation import raise_if_cancelled
from assistant_agent.runtime.tool_executor import ToolExecutor
from assistant_agent.runtime.chat_adapter import ChatAdapter
from assistant_agent.context.service import ContextService
from assistant_agent.runtime.event_sink import EventSink
from assistant_agent.observability.trace_store import TraceStore, trace_graph_node
from assistant_agent.runtime.assistant_graph_state import (
    AssistantTurnState,
    assistant_loop_state_from_turn_state,
    assistant_turn_state_from_loop_state,
)
from assistant_agent.runtime.state import AgentState


GraphState = dict[str, Any]
GraphStateT = TypeVar("GraphStateT", bound=dict[str, Any])


RUNTIME_STATE_KEYS = frozenset(
    {
        "tool_executor",
        "chat_adapter",
        "chat_turn",
        "context_service",
        "context_projector",
        "tool_result_handler",
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
    chat_turn: Callable[[Any], Any] | None = None
    context_service: ContextService | None = None
    context_projector: Callable[[Any], None] | None = None
    tool_result_handler: Callable[[Any, Any], None] | None = None
    trace_store: TraceStore | None = None
    event_sink: EventSink | None = None
    cancel_token: Any | None = None
    agent_state: AgentState | None = None


def bind_checkpointed_runtime_node(
    node_name: str,
    node_func: Callable[[GraphState], GraphState],
    *,
    trace: bool = True,
) -> Callable[[AssistantTurnState, Runtime[GraphRuntimeContext]], AssistantTurnState]:
    """Adapt a strict checkpoint state to a temporary legacy node invocation."""

    executable = trace_graph_node(node_name, node_func) if trace else node_func

    def wrapped(
        graph_state: AssistantTurnState,
        runtime: Runtime[GraphRuntimeContext],
    ) -> AssistantTurnState:
        runtime_context = runtime.context
        if runtime_context is None or runtime_context.agent_state is None:
            raise RuntimeError(f"{node_name} requires invocation-local AgentState")
        raise_if_cancelled(
            runtime_context.cancel_token,
            phase="before_node",
            node_name=node_name,
        )
        legacy_state = assistant_loop_state_from_turn_state(
            graph_state,
            runtime_state=runtime_context.agent_state,
        )
        enriched_state = _with_runtime_context(legacy_state, runtime_context)
        result = executable(enriched_state)
        raise_if_cancelled(
            runtime_context.cancel_token,
            phase="after_node",
            node_name=node_name,
            state=result.get("state") if isinstance(result, dict) else None,
        )
        profile = graph_state.get("profile", "standard")
        return assistant_turn_state_from_loop_state(result, profile=profile)

    return wrapped


def bind_runtime_node(
    node_name: str,
    node_func: Callable[[GraphStateT], GraphStateT],
    *,
    trace: bool = True,
) -> Callable[[GraphStateT, Runtime[GraphRuntimeContext]], GraphStateT]:
    """Return a node that injects runtime objects only during execution."""

    executable = trace_graph_node(node_name, node_func) if trace else node_func

    def wrapped(
        graph_state: GraphStateT,
        runtime: Runtime[GraphRuntimeContext],
    ) -> GraphStateT:
        runtime_context = runtime.context
        if runtime_context is None:
            raise RuntimeError(f"{node_name} requires GraphRuntimeContext")
        raise_if_cancelled(
            runtime_context.cancel_token, phase="before_node", node_name=node_name
        )
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


def _with_runtime_context(
    graph_state: GraphStateT, runtime_context: GraphRuntimeContext
) -> GraphStateT:
    enriched_state = dict(graph_state)
    enriched_state["tool_executor"] = runtime_context.tool_executor
    enriched_state["chat_adapter"] = runtime_context.chat_adapter
    if runtime_context.chat_turn is not None:
        enriched_state["chat_turn"] = runtime_context.chat_turn
    if runtime_context.context_service is not None:
        enriched_state["context_service"] = runtime_context.context_service
    if runtime_context.context_projector is not None:
        enriched_state["context_projector"] = runtime_context.context_projector
    if runtime_context.tool_result_handler is not None:
        enriched_state["tool_result_handler"] = runtime_context.tool_result_handler
    if runtime_context.trace_store is not None:
        enriched_state["trace_store"] = runtime_context.trace_store
    if runtime_context.event_sink is not None:
        enriched_state["event_sink"] = runtime_context.event_sink
    if runtime_context.cancel_token is not None:
        enriched_state["cancel_token"] = runtime_context.cancel_token
    return cast(GraphStateT, enriched_state)
