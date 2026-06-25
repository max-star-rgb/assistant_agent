"""LangGraph workflow with intent-based conditional routing."""

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
from multimodal_agent.agent.intent import IntentDetector
from multimodal_agent.agent.router import ToolRouter
from multimodal_agent.agent.state import AgentState
from multimodal_agent.agent.tool_executor import ToolExecutor
from multimodal_agent.memory.factory import create_memory_store
from multimodal_agent.memory.manager import MemoryManager
from multimodal_agent.agent.workflow import AgentWorkflow
from multimodal_agent.schemas.requests import UserRequest
from multimodal_agent.services.chat_adapter import create_chat_adapter
from multimodal_agent.services.trace_store import InMemoryTraceStore
from multimodal_agent.tools.registry import create_default_registry


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
    """Run the conditional LangGraph workflow and return the final AgentState."""

    registry = workflow.registry if workflow is not None else create_default_registry()
    tool_history = workflow.tool_history if workflow is not None else None
    memory_manager = MemoryManager(create_memory_store())
    state = AgentState.from_request(request)
    initial_state: AgentGraphState = {
        "request": request,
        "state": state,
        "intent_detector": workflow.intent_detector if workflow is not None else IntentDetector(),
        "router": workflow.router if workflow is not None else ToolRouter(),
        "tool_executor": ToolExecutor(
            registry=registry,
            tool_history=tool_history,
            context_metadata={"memory_manager": memory_manager},
        ),
        "chat_adapter": create_chat_adapter(),
        "memory_manager": memory_manager,
        "outputs_by_step": {},
        "current_step_index": 0,
        "trace_id": state.trace_id,
        "trace_store": InMemoryTraceStore(),
    }
    final_state = build_conditional_agent_graph().invoke(
        initial_state,
        config={
            "configurable": {
                "thread_id": state.run_id,
                "session_id": request.session_id,
                "user_id": request.user_id,
                "run_id": state.run_id,
            }
        },
    )
    return final_state["state"]
