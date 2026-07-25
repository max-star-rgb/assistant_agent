"""Assistant loop graph builder for ReAct-style reasoning."""

from typing import Any

from langgraph.graph import END, START, StateGraph

from assistant_agent.agent.assistant_loop_nodes import (
    AssistantLoopState,
    assistant_node,
    execute_requested_tool_node,
    route_after_assistant,
)
from assistant_agent.agent.graph_nodes import compose_response_node
from assistant_agent.agent.graph_runtime import GraphRuntimeContext, bind_runtime_node


def build_assistant_loop_graph(
    *,
    checkpointer: Any | None = None,
    runtime_context: GraphRuntimeContext | None = None,
) -> Any:
    """
    Build and compile the assistant loop graph.

    This is a ReAct-style graph:
        START -> assistant -> route -> finish -> END
                           -> execute_tool -> assistant
    """
    graph = StateGraph(AssistantLoopState)

    graph.add_node("assistant", bind_runtime_node("assistant", assistant_node, runtime_context))
    graph.add_node("execute_tool", bind_runtime_node("execute_tool", execute_requested_tool_node, runtime_context))
    graph.add_node("compose_response", bind_runtime_node("compose_response", compose_response_node, runtime_context))

    graph.add_edge(START, "assistant")

    graph.add_conditional_edges(
        "assistant",
        route_after_assistant,
        {
            "execute_tool": "execute_tool",
            "finish": "compose_response",
        },
    )

    graph.add_edge("execute_tool", "assistant")
    graph.add_edge("compose_response", END)

    return graph.compile(checkpointer=checkpointer)
