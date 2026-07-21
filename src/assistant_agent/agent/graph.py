"""LangGraph-backed linear agent workflow."""

from dataclasses import replace
from typing import Any

from langgraph.graph import END, START, StateGraph

from assistant_agent.agent.graph_nodes import (
    AgentGraphState,
    compose_response_node,
    detect_intent_node,
    load_memory_node,
    save_memory_node,
    execute_tools_node,
    route_tools_node,
)
from assistant_agent.agent.graph_runtime import GraphRuntimeContext, bind_runtime_node
from assistant_agent.agent.state import AgentState
from assistant_agent.agent.workflow import AgentWorkflow
from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.requests import UserRequest


def build_agent_graph(
    *,
    checkpointer: Any | None = None,
    runtime_context: GraphRuntimeContext | None = None,
) -> Any:
    """Build and compile the minimal LangGraph workflow."""

    graph = StateGraph(AgentGraphState)
    graph.add_node("load_memory", bind_runtime_node("load_memory", load_memory_node, runtime_context, trace=False))
    graph.add_node("detect_intent", bind_runtime_node("detect_intent", detect_intent_node, runtime_context, trace=False))
    graph.add_node("route_tools", bind_runtime_node("route_tools", route_tools_node, runtime_context, trace=False))
    graph.add_node("execute_tools", bind_runtime_node("execute_tools", execute_tools_node, runtime_context, trace=False))
    graph.add_node("compose_response", bind_runtime_node("compose_response", compose_response_node, runtime_context, trace=False))
    graph.add_node("save_memory", bind_runtime_node("save_memory", save_memory_node, runtime_context, trace=False))
    graph.add_edge(START, "load_memory")
    graph.add_edge("load_memory", "detect_intent")
    graph.add_edge("detect_intent", "route_tools")
    graph.add_edge("route_tools", "execute_tools")
    graph.add_edge("execute_tools", "compose_response")
    graph.add_edge("compose_response", "save_memory")
    graph.add_edge("save_memory", END)
    return graph.compile(checkpointer=checkpointer)


def run_agent_graph(request: UserRequest, workflow: AgentWorkflow | None = None) -> AgentState:
    """Run the compatibility graph through AgentGraphRuntime-owned dependencies."""

    from assistant_agent.agent.runtime import AgentGraphRuntime

    config = replace(ProviderConfig.from_env(), agent_graph_mode="conditional")
    return AgentGraphRuntime(
        config=config,
        registry=workflow.registry if workflow is not None else None,
        intent_detector=workflow.intent_detector if workflow is not None else None,
        router=workflow.router if workflow is not None else None,
        run_history=workflow.run_history if workflow is not None else None,
    ).run_state(request)
