"""Assistant loop graph builder for ReAct-style reasoning."""

from typing import Any

from langgraph.graph import END, START, StateGraph

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
