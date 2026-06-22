"""Assistant loop graph builder for ReAct-style reasoning."""

from typing import Any

from langgraph.graph import END, START, StateGraph

from multimodal_agent.agent.assistant_loop_nodes import (
    AssistantLoopState,
    assistant_node,
    execute_requested_tool_node,
    route_after_assistant,
)
from multimodal_agent.agent.graph_nodes import compose_response_node, load_memory_node, save_memory_node
from multimodal_agent.services.trace_store import trace_graph_node


def build_assistant_loop_graph() -> Any:
    """
    Build and compile the assistant loop graph.

    This is a ReAct-style graph:
        START -> load_memory -> assistant -> route -> finish -> END
                                      -> execute_tool -> assistant
    """
    graph = StateGraph(AssistantLoopState)

    graph.add_node("load_memory", trace_graph_node("load_memory", load_memory_node))
    graph.add_node("assistant", trace_graph_node("assistant", assistant_node))
    graph.add_node("execute_tool", trace_graph_node("execute_tool", execute_requested_tool_node))
    graph.add_node("compose_response", trace_graph_node("compose_response", compose_response_node))
    graph.add_node("save_memory", trace_graph_node("save_memory", save_memory_node))

    graph.add_edge(START, "load_memory")
    graph.add_edge("load_memory", "assistant")

    graph.add_conditional_edges(
        "assistant",
        route_after_assistant,
        {
            "execute_tool": "execute_tool",
            "finish": "compose_response",
        },
    )

    graph.add_edge("execute_tool", "assistant")
    graph.add_edge("compose_response", "save_memory")
    graph.add_edge("save_memory", END)

    return graph.compile()
