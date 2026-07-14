"""Reusable LangGraph node functions for agent execution."""

from inspect import signature
from time import perf_counter
from typing import NotRequired, TypedDict

from assistant_agent.agent.intent import IntentDetector
from assistant_agent.agent.llm_event_mapping import stream_delta_to_agent_event
from assistant_agent.agent.prompt_builder import build_direct_chat_request, build_text_capability_output
from assistant_agent.agent.router import ToolRouter
from assistant_agent.agent.state import AgentError, AgentState
from assistant_agent.agent.response_composer import compose_response, save_demo_memory
from assistant_agent.agent.tool_executor import ToolExecutor
from assistant_agent.agent.tool_input_builder import build_tool_input
from assistant_agent.memory.manager import MemoryManager
from assistant_agent.schemas.capabilities import canonical_intent
from assistant_agent.schemas.planning import TaskPlan
from assistant_agent.schemas.requests import AgentResponse, UserRequest
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.chat_adapter import ChatAdapter
from assistant_agent.services.memory_observability import load_memory_with_trace, save_memory_with_trace
from assistant_agent.services.response_observability import append_response_final_event
from assistant_agent.services.trace_store import TraceStore


class AgentGraphState(TypedDict):
    """Mutable state passed between LangGraph nodes."""

    request: UserRequest
    state: AgentState
    intent_detector: NotRequired[IntentDetector]
    router: NotRequired[ToolRouter]
    tool_executor: NotRequired[ToolExecutor]
    chat_adapter: NotRequired[ChatAdapter]
    memory_manager: NotRequired[MemoryManager]
    outputs_by_step: dict[str, ToolResult]
    current_step_index: int
    trace_id: NotRequired[str]
    trace_store: NotRequired[TraceStore]
    event_sink: NotRequired[object]
    current_node_name: NotRequired[str]


def load_memory_node(graph_state: AgentGraphState) -> AgentGraphState:
    load_memory_with_trace(
        manager=_memory_manager(graph_state),
        trace_store=graph_state.get("trace_store"),
        trace_id=graph_state.get("trace_id"),
        node_name=graph_state.get("current_node_name", "load_memory"),
        state=graph_state["state"],
        request=graph_state["request"],
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
    plan = _route_with_optional_request(router, state.intent, graph_state["request"])
    state.set_plan(plan)
    state.selected_tools = _select_tools_with_optional_request(router, state.intent, graph_state["request"])
    return graph_state


def route_by_intent(graph_state: AgentGraphState) -> str:
    """Return the next node name only; business work happens in nodes."""

    intent = graph_state["state"].intent
    if intent is None:
        return "chat_node"
    capability = canonical_intent(intent.intent)
    if capability in {"image_understanding", "video_understanding"}:
        return "vision_node"
    if capability in {"product_search", "web_search"}:
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
    state = graph_state["state"]
    intent = state.intent
    if intent is not None and canonical_intent(intent.intent) == "direct_chat":
        input_size_bytes = len((graph_state["request"].text or "").encode("utf-8"))
        budget_error = state.provider_budget.check_before_call(
            capability="direct_chat",
            input_size_bytes=input_size_bytes,
        )
        if budget_error is not None:
            errors = [budget_error.model_dump(mode="json")]
            contract = build_text_capability_output(
                capability="direct_chat",
                status="failed",
                errors=errors,
            )
            state.errors.append(
                AgentError(
                    message=budget_error.message,
                    source="direct_chat",
                    details={
                        "code": budget_error.code,
                        "recovery_action": "stop_with_error",
                        "retryable": False,
                        "provider_budget": state.provider_budget.summary(),
                    },
                )
            )
            state.response = AgentResponse(
                message=f"处理失败：{budget_error.code}: {budget_error.message}",
                data={
                    "intent": state.intent.intent if state.intent else None,
                    "tool_count": len(state.tool_calls),
                    "errors": errors,
                    "contract": contract,
                    "provider_budget": state.provider_budget.summary(),
                },
            )
            state.status = "failed"
            return graph_state
        memory_summaries = [item.summary for item in state.memory_context]
        memory_context_text = state.request.metadata.get("memory_context_text", "")
        chat_request = _with_response_stream_callback(
            build_direct_chat_request(
                graph_state["request"],
                memory_context=memory_summaries,
            ),
            graph_state,
            source="direct_chat",
        )
        result = graph_state["chat_adapter"].chat(chat_request)
        state.provider_budget.record_call(
            run_id=state.run_id,
            capability="direct_chat",
            provider=result.provider,
            model=result.model,
            input_size_bytes=input_size_bytes,
            latency_ms=result.latency_ms,
            status="succeeded" if result.success else "failed",
        )
        message = (
            result.response_text
            if result.success
            else f"处理失败：{result.errors[0].code}: {result.errors[0].message}"
        )
        if result.success and memory_summaries:
            message = f"{message}；参考记忆：{memory_summaries[0]}"
        errors = [error.model_dump(mode="json") for error in result.errors]
        contract = build_text_capability_output(
            capability="direct_chat",
            status="succeeded" if result.success else "failed",
            output_ref=result.output_ref,
            data={"response_text": result.response_text, "provider": result.provider, "model": result.model},
            errors=errors,
        )
        state.set_response(
            AgentResponse(
                message=message,
                data={
                    "intent": state.intent.intent if state.intent else None,
                    "tool_count": len(state.tool_calls),
                    "provider": result.provider,
                    "model": result.model,
                    "usage": result.usage,
                    "output_ref": result.output_ref,
                    "memory_context_count": len(state.memory_context),
                    "memory_context_summaries": memory_summaries,
                    "memory_context_text": memory_context_text,
                    "errors": errors,
                    "contract": contract,
                    "provider_budget": state.provider_budget.summary(),
                },
                output_refs=[result.output_ref] if result.output_ref else [],
            )
        )
    return graph_state


def compose_response_node(graph_state: AgentGraphState) -> AgentGraphState:
    state = graph_state["state"]
    if state.status != "failed":
        save_demo_memory(graph_state["request"], state, graph_state["tool_executor"])
    response_started_at = perf_counter()
    response = compose_response(state)
    if state.status == "failed":
        state.response = response
    else:
        state.set_response(response)
    append_response_final_event(
        trace_store=graph_state.get("trace_store"),
        trace_id=graph_state.get("trace_id"),
        node_name=graph_state.get("current_node_name", "compose_response"),
        state=state,
        source="compose_response",
        latency_ms=int((perf_counter() - response_started_at) * 1000),
    )
    return graph_state


def save_memory_node(graph_state: AgentGraphState) -> AgentGraphState:
    if _is_assistant_loop_state(graph_state) and not _uses_mock_chat_adapter(graph_state):
        skip_reason = "assistant_loop_memory_writes_are_llm_tool_calls"
        graph_state["state"].request.metadata["auto_task_summary_memory"] = {
            "skipped": True,
            "reason": skip_reason,
        }
        save_memory_with_trace(
            manager=_memory_manager(graph_state),
            trace_store=graph_state.get("trace_store"),
            trace_id=graph_state.get("trace_id"),
            node_name=graph_state.get("current_node_name", "save_memory"),
            state=graph_state["state"],
            skipped_reason=skip_reason,
        )
        return graph_state
    save_memory_with_trace(
        manager=_memory_manager(graph_state),
        trace_store=graph_state.get("trace_store"),
        trace_id=graph_state.get("trace_id"),
        node_name=graph_state.get("current_node_name", "save_memory"),
        state=graph_state["state"],
    )
    return graph_state


def _memory_manager(graph_state: AgentGraphState) -> MemoryManager:
    return graph_state["memory_manager"]


def _is_assistant_loop_state(graph_state: AgentGraphState) -> bool:
    return "assistant_iterations" in graph_state or "assistant_decision" in graph_state


def _uses_mock_chat_adapter(graph_state: AgentGraphState) -> bool:
    chat_adapter = graph_state.get("chat_adapter")
    return getattr(chat_adapter, "provider", "") == "mock"


def _with_response_stream_callback(
    request,
    graph_state: AgentGraphState,
    *,
    source: str,
):
    event_sink = graph_state.get("event_sink")
    if event_sink is None:
        return request
    state = graph_state["state"]

    def emit_delta(text: str, payload: dict) -> None:
        if not hasattr(event_sink, "emit"):
            return
        event = stream_delta_to_agent_event(
            text,
            dict(payload),
            session_id=state.session_id,
            run_id=state.run_id,
            source=source,
        )
        if event is None:
            return
        event_sink.emit(event)

    return request.model_copy(update={"stream_callback": emit_delta})


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


def _route_with_optional_request(router: ToolRouter, intent, request: UserRequest) -> TaskPlan:
    if len(signature(router.route).parameters) >= 2:
        return router.route(intent, request)
    return router.route(intent)


def _select_tools_with_optional_request(router: ToolRouter, intent, request: UserRequest):
    if len(signature(router.select_tools).parameters) >= 2:
        return router.select_tools(intent, request)
    return router.select_tools(intent)
