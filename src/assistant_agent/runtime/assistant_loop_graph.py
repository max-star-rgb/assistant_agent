"""Assistant loop graph builders for standalone and namespaced composition."""

from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from assistant_agent.runtime.assistant_loop_nodes import (
    assistant_node,
    await_input_node,
    compose_response_node,
    execute_requested_tool_node,
)
from assistant_agent.runtime.assistant_graph_state import (
    AssistantTurnState,
    route_after_await_input_turn_state,
    route_after_assistant_turn_state,
    validate_assistant_turn_state,
)
from assistant_agent.runtime.assistant_graph_profiles import AssistantGraphProfileName
from assistant_agent.runtime.graph_runtime import (
    GraphRuntimeContext,
    bind_checkpointed_runtime_node,
)


def build_assistant_loop_graph(
    *,
    checkpointer: Any | None = None,
    profile: AssistantGraphProfileName = "standard",
    graph_name: str = "AssistantTurnGraph",
) -> Any:
    """
    Build and compile the assistant loop graph.

    This is a ReAct-style graph:
        START -> assistant -> route -> finish -> END
                           -> execute_tool -> assistant
    """
    graph = StateGraph(AssistantTurnState, context_schema=GraphRuntimeContext)

    graph.add_node(
        "assistant",
        bind_checkpointed_runtime_node(
            "assistant",
            assistant_node,
            expected_profile=profile,
        ),
    )
    graph.add_node(
        "await_input",
        await_input_node,
    )
    graph.add_node(
        "execute_tool",
        bind_checkpointed_runtime_node(
            "execute_tool",
            execute_requested_tool_node,
            expected_profile=profile,
        ),
    )
    graph.add_node(
        "compose_response",
        bind_checkpointed_runtime_node(
            "compose_response",
            compose_response_node,
            expected_profile=profile,
        ),
    )

    graph.add_edge(START, "assistant")

    graph.add_conditional_edges(
        "assistant",
        route_after_assistant_turn_state,
        {
            "await_input": "await_input",
            "execute_tool": "execute_tool",
            "finish": "compose_response",
        },
    )

    graph.add_conditional_edges(
        "await_input",
        route_after_await_input_turn_state,
        {
            "execute_tool": "execute_tool",
            "assistant": "assistant",
        },
    )

    graph.add_edge("execute_tool", "assistant")
    graph.add_edge("compose_response", END)

    return graph.compile(checkpointer=checkpointer, name=graph_name)


def build_namespaced_assistant_loop_graph(
    *,
    state_schema: type,
    context_schema: type,
    child_state_key: str,
    runtime_context_resolver: Callable[
        [Mapping[str, object], AssistantTurnState, object], GraphRuntimeContext
    ],
    profile: AssistantGraphProfileName,
    graph_name: str,
) -> Any:
    """Compile the same native loop against one explicitly nested state channel.

    This is used when a parent graph owns channels such as ``status`` and
    ``graph_name`` whose meanings differ from AssistantTurnState.  It invokes
    no graph from inside a node: the returned compiled graph is itself added as
    a native subgraph by its caller.
    """

    def nested_node(node_name: str, node_func: Callable[..., AssistantTurnState]):
        bound = bind_checkpointed_runtime_node(
            node_name,
            node_func,
            expected_profile=profile,
        )

        def invoke(
            state: Mapping[str, object],
            runtime: Runtime[object],
        ) -> dict[str, AssistantTurnState]:
            child = state.get(child_state_key)
            if not isinstance(child, Mapping):
                raise ValueError(f"{child_state_key} must contain AssistantTurnState")
            child_state = validate_assistant_turn_state(child)
            child_context = runtime_context_resolver(
                state,
                child_state,
                runtime.context,
            )
            child_runtime = replace(runtime, context=child_context)
            return {child_state_key: bound(child_state, child_runtime)}

        return invoke

    def nested_await(
        state: Mapping[str, object],
        runtime: Runtime[object],
    ) -> dict[str, AssistantTurnState]:
        child = state.get(child_state_key)
        if not isinstance(child, Mapping):
            raise ValueError(f"{child_state_key} must contain AssistantTurnState")
        child_state = validate_assistant_turn_state(child)
        child_context = runtime_context_resolver(state, child_state, runtime.context)
        return {
            child_state_key: await_input_node(
                child_state,
                replace(runtime, context=child_context),
            )
        }

    def route_assistant(state: Mapping[str, object]) -> str:
        return route_after_assistant_turn_state(state[child_state_key])  # type: ignore[arg-type]

    def route_await(state: Mapping[str, object]) -> str:
        return route_after_await_input_turn_state(state[child_state_key])  # type: ignore[arg-type]

    graph = StateGraph(state_schema, context_schema=context_schema)
    graph.add_node("assistant", nested_node("assistant", assistant_node))
    graph.add_node("await_input", nested_await)
    graph.add_node(
        "execute_tool",
        nested_node("execute_tool", execute_requested_tool_node),
    )
    graph.add_node(
        "compose_response",
        nested_node("compose_response", compose_response_node),
    )
    graph.add_edge(START, "assistant")
    graph.add_conditional_edges(
        "assistant",
        route_assistant,
        {
            "await_input": "await_input",
            "execute_tool": "execute_tool",
            "finish": "compose_response",
        },
    )
    graph.add_conditional_edges(
        "await_input",
        route_await,
        {"execute_tool": "execute_tool", "assistant": "assistant"},
    )
    graph.add_edge("execute_tool", "assistant")
    graph.add_edge("compose_response", END)
    return graph.compile(checkpointer=None, name=graph_name)
