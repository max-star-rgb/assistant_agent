"""LangGraph workflow with intent-based conditional routing."""

from dataclasses import replace
from typing import Any

from langgraph.graph import END, START, StateGraph

from multimodal_agent.agent.graph_nodes import (
    AgentGraphState,
    chat_node,
    compose_response_node,
    detect_intent_node,
    load_memory_node,
    save_memory_node,
    route_by_intent,
    run_first_tool_node,
    plan_steps_node,
    select_next_step_node,
    execute_step_node,
    should_continue,
)
from multimodal_agent.agent.graph_runtime import GraphRuntimeContext, bind_runtime_node
from multimodal_agent.agent.state import AgentState
from multimodal_agent.agent.workflow import AgentWorkflow
from multimodal_agent.config import ProviderConfig
from multimodal_agent.schemas.requests import UserRequest


def build_conditional_agent_graph(
    *,
    checkpointer: Any | None = None,
    runtime_context: GraphRuntimeContext | None = None,
) -> Any:
    """Build and compile a conditional LangGraph workflow."""

    graph = StateGraph(AgentGraphState)
    graph.add_node("load_memory", bind_runtime_node("load_memory", load_memory_node, runtime_context))
    graph.add_node("detect_intent", bind_runtime_node("detect_intent", detect_intent_node, runtime_context))
    graph.add_node("vision_node", bind_runtime_node("vision_node", run_first_tool_node, runtime_context))
    graph.add_node("search_node", bind_runtime_node("search_node", run_first_tool_node, runtime_context))
    graph.add_node("compare_node", bind_runtime_node("compare_node", run_first_tool_node, runtime_context))
    graph.add_node("image_generation_node", bind_runtime_node("image_generation_node", run_first_tool_node, runtime_context))
    graph.add_node("render_node", bind_runtime_node("render_node", run_first_tool_node, runtime_context))
    graph.add_node("memory_node", bind_runtime_node("memory_node", run_first_tool_node, runtime_context))
    graph.add_node("chat_node", bind_runtime_node("chat_node", chat_node, runtime_context))
    graph.add_node("plan_steps", bind_runtime_node("plan_steps", plan_steps_node, runtime_context))
    # Explicit loop nodes kept trace-wrapped:
    # graph.add_node("select_next_step", select_next_step_node)
    # graph.add_node("execute_step", execute_step_node)
    graph.add_node("select_next_step", bind_runtime_node("select_next_step", select_next_step_node, runtime_context))
    graph.add_node("execute_step", bind_runtime_node("execute_step", execute_step_node, runtime_context))
    graph.add_node("compose_response", bind_runtime_node("compose_response", compose_response_node, runtime_context))
    graph.add_node("save_memory", bind_runtime_node("save_memory", save_memory_node, runtime_context))
    graph.add_edge(START, "load_memory")
    graph.add_edge("load_memory", "detect_intent")
    graph.add_conditional_edges(
        "detect_intent",
        route_by_intent,
        {
            "vision_node": "vision_node",
            "search_node": "search_node",
            "compare_node": "compare_node",
            "image_generation_node": "image_generation_node",
            "render_node": "render_node",
            "memory_node": "memory_node",
            "chat_node": "chat_node",
            "multi_tool_node": "plan_steps",
        },
    )
    for node_name in (
        "vision_node",
        "search_node",
        "compare_node",
        "image_generation_node",
        "render_node",
        "memory_node",
        "chat_node",
    ):
        graph.add_edge(node_name, "compose_response")
    graph.add_edge("plan_steps", "select_next_step")
    graph.add_edge("select_next_step", "execute_step")
    graph.add_conditional_edges(
        "execute_step",
        should_continue,
        {
            "continue": "select_next_step",
            "finish": "compose_response",
        },
    )
    graph.add_edge("compose_response", "save_memory")
    graph.add_edge("save_memory", END)
    return graph.compile(checkpointer=checkpointer)


def run_conditional_agent_graph(request: UserRequest, workflow: AgentWorkflow | None = None) -> AgentState:
    """Run the conditional graph through AgentGraphRuntime-owned dependencies."""

    from multimodal_agent.agent.runtime import AgentGraphRuntime

    config = replace(ProviderConfig.from_env(), agent_graph_mode="conditional")
    return AgentGraphRuntime(
        config=config,
        registry=workflow.registry if workflow is not None else None,
        intent_detector=workflow.intent_detector if workflow is not None else None,
        router=workflow.router if workflow is not None else None,
        run_history=workflow.run_history if workflow is not None else None,
        tool_history=workflow.tool_history if workflow is not None else None,
    ).run_state(request)
