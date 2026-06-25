"""LangGraph-backed linear agent workflow."""

from typing import Any

from langgraph.graph import END, START, StateGraph

from multimodal_agent.agent.graph_nodes import (
    AgentGraphState,
    compose_response_node,
    detect_intent_node,
    load_memory_node,
    save_memory_node,
    execute_tools_node,
    route_tools_node,
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
from multimodal_agent.tools.registry import create_default_registry


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
    """Run the compiled LangGraph workflow and return the final AgentState."""

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
    }
    final_state = build_agent_graph().invoke(
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
