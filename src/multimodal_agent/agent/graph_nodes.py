"""Reusable LangGraph node functions for agent execution."""

from datetime import datetime, timezone
from uuid import uuid4
from typing import NotRequired, TypedDict

from multimodal_agent.agent.intent import IntentDetector
from multimodal_agent.agent.router import ToolRouter
from multimodal_agent.agent.state import AgentState
from multimodal_agent.agent.response_composer import compose_response, save_demo_memory
from multimodal_agent.agent.tool_executor import ToolExecutor
from multimodal_agent.agent.tool_input_builder import build_tool_input
from multimodal_agent.memory.retrieval import MemoryRetrievalStrategy, format_memory_context
from multimodal_agent.memory.store import MemoryStore
from multimodal_agent.schemas.capabilities import canonical_intent
from multimodal_agent.schemas.memory import MemoryItem, MemoryQuery
from multimodal_agent.schemas.requests import UserRequest
from multimodal_agent.schemas.tools import ToolResult
from multimodal_agent.services.trace_store import TraceStore


class AgentGraphState(TypedDict):
    """Mutable state passed between LangGraph nodes."""

    request: UserRequest
    state: AgentState
    intent_detector: IntentDetector
    router: ToolRouter
    tool_executor: ToolExecutor
    memory_store: MemoryStore
    outputs_by_step: dict[str, ToolResult]
    current_step_index: int
    trace_id: NotRequired[str]
    trace_store: NotRequired[TraceStore]
    current_node_name: NotRequired[str]


def load_memory_node(graph_state: AgentGraphState) -> AgentGraphState:
    state = graph_state["state"]
    query = MemoryQuery(
        user_id=state.user_id,
        query=graph_state["request"].text or "",
        top_k=5,
        max_context_chars=500,
    )
    state.memory_context = MemoryRetrievalStrategy(graph_state["memory_store"]).retrieve(query)
    state.request.metadata["memory_context_text"] = format_memory_context(
        state.memory_context,
        max_chars=query.max_context_chars,
    )
    return graph_state


def detect_intent_node(graph_state: AgentGraphState) -> AgentGraphState:
    intent = graph_state["intent_detector"].detect(graph_state["request"])
    graph_state["state"].set_intent(intent)
    return graph_state


def route_tools_node(graph_state: AgentGraphState) -> AgentGraphState:
    state = graph_state["state"]
    if state.intent is None:
        raise ValueError("Cannot route tools before intent detection")
    router = graph_state["router"]
    plan = router.route(state.intent)
    state.set_plan(plan)
    state.selected_tools = router.select_tools(state.intent)
    return graph_state


def route_by_intent(graph_state: AgentGraphState) -> str:
    """Return the next node name only; business work happens in nodes."""

    intent = graph_state["state"].intent
    if intent is None:
        return "chat_node"
    capability = canonical_intent(intent.intent)
    if capability in {"image_understanding", "video_understanding"}:
        return "vision_node"
    if capability == "product_search":
        return "search_node"
    if capability == "price_compare":
        return "compare_node"
    if capability == "image_generation":
        return "image_generation_node"
    if capability == "render_3d":
        return "render_node"
    if capability in {"memory_retrieval", "memory_save"}:
        return "memory_node"
    if capability == "multi_step_orchestration":
        return "multi_tool_node"
    return "chat_node"


def execute_tools_node(graph_state: AgentGraphState) -> AgentGraphState:
    route_tools_node(graph_state)
    return _run_planned_tools(graph_state, stop_after_first=False)


def plan_steps_node(graph_state: AgentGraphState) -> AgentGraphState:
    route_tools_node(graph_state)
    graph_state["current_step_index"] = 0
    return graph_state


def select_next_step_node(graph_state: AgentGraphState) -> AgentGraphState:
    return graph_state


def execute_step_node(graph_state: AgentGraphState) -> AgentGraphState:
    request = graph_state["request"]
    state = graph_state["state"]
    outputs_by_step = graph_state["outputs_by_step"]
    index = graph_state["current_step_index"]
    if state.plan is None or index >= len(state.plan.steps):
        return graph_state

    step = state.plan.steps[index]
    if step.tool_name is not None:
        tool_input = build_tool_input(step.action, request, outputs_by_step)
        result = graph_state["tool_executor"].run_tool(
            state,
            step.step_id,
            step.tool_name,
            tool_input,
            step=step,
            trace_store=graph_state.get("trace_store"),
            trace_id=graph_state.get("trace_id"),
            node_name=graph_state.get("current_node_name"),
        )
        if result.success:
            outputs_by_step[step.step_id] = result
    graph_state["current_step_index"] = index + 1
    return graph_state


def should_continue(graph_state: AgentGraphState) -> str:
    state = graph_state["state"]
    if state.status == "failed":
        return "finish"
    if state.plan is None:
        return "finish"
    if graph_state["current_step_index"] >= len(state.plan.steps):
        return "finish"
    return "continue"


def run_first_tool_node(graph_state: AgentGraphState) -> AgentGraphState:
    route_tools_node(graph_state)
    return _run_planned_tools(graph_state, stop_after_first=True)


def chat_node(graph_state: AgentGraphState) -> AgentGraphState:
    route_tools_node(graph_state)
    return graph_state


def compose_response_node(graph_state: AgentGraphState) -> AgentGraphState:
    state = graph_state["state"]
    if state.status != "failed":
        save_demo_memory(graph_state["request"], state, graph_state["tool_executor"])
    response = compose_response(state)
    if state.status == "failed":
        state.response = response
    else:
        state.set_response(response)
    return graph_state


def save_memory_node(graph_state: AgentGraphState) -> AgentGraphState:
    state = graph_state["state"]
    if state.status == "completed" and state.response is not None:
        graph_state["memory_store"].save(_memory_from_state(state))
    return graph_state


def _memory_from_state(state: AgentState) -> MemoryItem:
    return MemoryItem(
        memory_id=f"run_memory_{uuid4().hex}",
        user_id=state.user_id,
        memory_type="task",
        summary=state.response.message if state.response else "Agent run completed.",
        content={
            "session_id": state.session_id,
            "query": state.request.text,
            "intent": state.intent.intent if state.intent else None,
            "selected_tools": [tool.tool_name for tool in state.selected_tools],
            "final_response": state.response.message if state.response else None,
        },
        created_at=datetime.now(timezone.utc),
    )


def _run_planned_tools(graph_state: AgentGraphState, stop_after_first: bool) -> AgentGraphState:
    request = graph_state["request"]
    state = graph_state["state"]
    outputs_by_step = graph_state["outputs_by_step"]
    tool_executor = graph_state["tool_executor"]
    if state.plan is None:
        return graph_state

    for step in state.plan.steps:
        if step.tool_name is None:
            continue
        tool_input = build_tool_input(step.action, request, outputs_by_step)
        result = tool_executor.run_tool(
            state,
            step.step_id,
            step.tool_name,
            tool_input,
            step=step,
            trace_store=graph_state.get("trace_store"),
            trace_id=graph_state.get("trace_id"),
            node_name=graph_state.get("current_node_name"),
        )
        if result.success:
            outputs_by_step[step.step_id] = result
        elif state.status == "failed" and not stop_after_first:
            break
        if stop_after_first:
            break
    return graph_state
