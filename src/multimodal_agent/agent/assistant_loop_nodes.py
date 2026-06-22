"""Assistant loop nodes for ReAct-style reasoning."""

import json
from inspect import signature
from typing import Any, NotRequired, TypedDict, cast

from multimodal_agent.agent.action_validator import ActionValidator
from multimodal_agent.agent.intent import IntentDetector
from multimodal_agent.agent.loop_guard import LoopGuard, LoopGuardDecision
from multimodal_agent.agent.prompt_builder import build_direct_chat_request, build_text_capability_output
from multimodal_agent.agent.router import ToolRouter
from multimodal_agent.agent.state import AgentError, AgentState
from multimodal_agent.agent.tool_executor import ToolExecutor
from multimodal_agent.memory.retrieval import MemoryRetrievalStrategy, format_memory_context
from multimodal_agent.schemas.assistant_decision import AssistantDecision
from multimodal_agent.schemas.capabilities import canonical_intent
from multimodal_agent.schemas.events import AgentEvent
from multimodal_agent.schemas.requests import AgentResponse, UserRequest
from multimodal_agent.schemas.tool_observation import (
    ToolObservation,
    observation_from_tool_result,
    rejected_observation,
)
from multimodal_agent.schemas.tools import ToolResult
from multimodal_agent.services.chat_adapter import ChatAdapter, ChatRequest
from multimodal_agent.services.trace_store import TraceEvent, sanitize_trace_value


class AssistantLoopState(TypedDict):
    """State for the assistant loop graph."""

    request: UserRequest
    state: AgentState
    intent_detector: NotRequired[IntentDetector]
    router: NotRequired[ToolRouter]
    tool_executor: ToolExecutor
    chat_adapter: ChatAdapter
    memory_store: Any
    outputs_by_step: dict[str, ToolResult]
    current_step_index: int
    trace_id: NotRequired[str]
    trace_store: NotRequired[Any]
    assistant_decision: NotRequired[AssistantDecision | None]
    assistant_iterations: NotRequired[int]
    tool_observations: NotRequired[list[dict[str, Any]]]
    current_node_name: NotRequired[str]


def _mock_assistant_decision(
    request: UserRequest,
    tool_observations: list[dict[str, Any]],
    available_tools: list[str],
    iteration: int,
    max_iterations: int,
) -> AssistantDecision:
    """
    Mock assistant decision logic for testing/demo purposes.

    This implements simple rule-based decisions when using mock adapter.
    """
    user_query = request.text or ""
    query_lower = user_query.lower()

    # If we have tool observations, synthesize a final answer
    if tool_observations:
        last_obs = tool_observations[-1]
        tool_name = last_obs.get("tool_name", "tool")

        if last_obs.get("success"):
            return AssistantDecision(
                type="final_answer",
                message=f"已完成 {tool_name} 调用！结果已准备好。",
                reason=f"工具 {tool_name} 执行成功，结果足够回答用户问题",
            )
        else:
            return AssistantDecision(
                type="final_answer",
                message=f"{tool_name} 执行遇到问题，但我会尽力回答。",
                reason=f"工具 {tool_name} 执行失败，提供 fallback 回答",
            )

    # Check for image generation requests
    image_keywords = ["生成", "图片", "海报", "画图", "image", "generate", "draw"]
    if any(kw in query_lower for kw in image_keywords) and "image_generation" in available_tools:
        return AssistantDecision(
            type="tool_call",
            tool_name="image_generation",
            tool_input={"prompt": user_query, "style": "realistic"},
            reason="用户需要生成图片，调用 image_generation 工具",
        )

    # Check for product search requests
    search_keywords = ["搜索", "找", "商品", "search", "product", "find"]
    if any(kw in query_lower for kw in search_keywords) and "product_search" in available_tools:
        return AssistantDecision(
            type="tool_call",
            tool_name="product_search",
            tool_input={"query": user_query, "limit": 5},
            reason="用户需要搜索商品，调用 product_search 工具",
        )

    # Check for vision understanding requests
    vision_keywords = ["看", "图片里", "图中", "识别", "vision", "image", "recognize"]
    has_images = bool(request.image_ids)
    if (any(kw in query_lower for kw in vision_keywords) or has_images) and "vision_understanding" in available_tools:
        return AssistantDecision(
            type="tool_call",
            tool_name="vision_understanding",
            tool_input={"prompt": user_query, "image_ids": request.image_ids or []},
            reason="用户需要理解图片内容，调用 vision_understanding 工具",
        )

    # Check for video understanding requests
    has_videos = bool(request.video_ids)
    if has_videos and "video_understanding" in available_tools:
        return AssistantDecision(
            type="tool_call",
            tool_name="video_understanding",
            tool_input={"prompt": user_query, "video_ids": request.video_ids or []},
            reason="用户上传了视频，调用 video_understanding 工具",
        )

    # Check for price compare requests
    compare_keywords = ["比价", "比较价格", "compare", "price"]
    if any(kw in query_lower for kw in compare_keywords) and "price_compare" in available_tools:
        return AssistantDecision(
            type="tool_call",
            tool_name="price_compare",
            tool_input={"query": user_query},
            reason="用户需要比较价格，调用 price_compare 工具",
        )

    # Check for render requests
    render_keywords = ["渲染", "3d", "render", "展示"]
    if any(kw in query_lower for kw in render_keywords) and "render_3d" in available_tools:
        return AssistantDecision(
            type="tool_call",
            tool_name="render_3d",
            tool_input={"prompt": user_query, "scene": "modern"},
            reason="用户需要 3D 渲染，调用 render_3d 工具",
        )

    # Check for ambiguous queries that need follow-up
    if len(user_query.strip()) < 3 or user_query.strip() in ["这个", "那个", "?", "？", "help", "帮助"]:
        return AssistantDecision(
            type="ask_followup",
            message="你需要什么帮助？可以告诉我你想做什么，比如：\n- 生成图片\n- 搜索商品\n- 理解图片/视频内容",
            reason="用户查询太短或太模糊，需要更多信息",
        )

    # Default: direct answer
    return AssistantDecision(
        type="final_answer",
        message=f"你好！我是你的 AI 助手。我可以帮你：\n- 生成图片\n- 搜索和比较商品\n- 理解图片和视频内容\n- 3D 渲染展示\n\n你的问题：{user_query}",
        reason="不需要调用工具，直接回答用户",
    )


def assistant_node(graph_state: AssistantLoopState) -> AssistantLoopState:
    """
    Central assistant reasoning node.

    Reads the request, memory, tool observations, and decides the next action.
    """
    state = graph_state["state"]
    chat_adapter = graph_state["chat_adapter"]
    iterations = graph_state.get("assistant_iterations", 0)
    tool_observations = graph_state.get("tool_observations", [])
    max_iterations = _get_max_tool_iterations()

    if state.status == "completed" and state.response is not None:
        decision = AssistantDecision(
            type="final_answer",
            message=state.response.message,
            reason="Run already completed before the next assistant turn.",
        )
        _record_react_decision(graph_state, decision, iterations)
        return {
            **graph_state,
            "assistant_decision": decision,
            "assistant_iterations": iterations + 1,
        }

    # Get available tools
    try:
        tool_descriptions = graph_state["tool_executor"].registry.describe_tools()
        available_tools = [t["name"] for t in tool_descriptions]
    except Exception:
        available_tools = []

    # Get memory context
    memory_context = state.memory_context
    memory_summaries = [item.summary for item in memory_context] if memory_context else []

    # Build prompt
    request = graph_state["request"]
    user_query = request.text or ""

    # Check if this is a mock adapter - if so, use our rule-based logic
    is_mock = _is_mock_chat_adapter(chat_adapter)

    if is_mock:
        _ensure_rule_plan(graph_state)
        # Use mock rule-based decision for predictable testing
        decision = _mock_assistant_decision_from_plan(
            request=request,
            tool_observations=tool_observations,
            state=state,
            outputs_by_step=graph_state["outputs_by_step"],
        )
        if decision.type == "tool_call" and iterations + 1 >= max_iterations:
            decision = _max_iteration_final_answer(max_iterations)
    else:
        # Use real LLM adapter
        prompt = _build_assistant_prompt(
            request=request,
            memory_summaries=memory_summaries,
            memory_text="",
            observations=tool_observations,
            tools_desc=tool_descriptions,
            iteration=iterations,
        )

        chat_request = ChatRequest(
            user_id=state.user_id,
            session_id=state.session_id,
            user_query=prompt,
            temperature=0.2,
            max_tokens=1024,
        )

        result = chat_adapter.chat(chat_request)
        response_text = result.response_text if result.success else ""

        decision = AssistantDecision.from_llm_output(response_text)
        if decision.type == "tool_call" and iterations + 1 >= max_iterations:
            decision = _request_final_answer_after_tool_limit(
                chat_adapter=chat_adapter,
                state=state,
                request=request,
                observations=tool_observations,
                iteration=iterations,
                max_iterations=max_iterations,
            )

    # Check max iterations - force final answer if exceeded
    if iterations >= max_iterations and decision.type == "tool_call":
        decision = _max_iteration_final_answer(max_iterations)

    # Update state
    if decision.reason == "Empty or whitespace-only output.":
        guard = LoopGuard(state.request.metadata).record_empty_decision()
        if guard.triggered:
            decision = _guard_final_answer(guard)
            _record_loop_guard(graph_state, guard)
    _record_react_decision(graph_state, decision, iterations)
    if decision.type in ("final_answer", "ask_followup"):
        if decision.type == "ask_followup" or not state.tool_results:
            if _is_direct_chat_state(state):
                _set_direct_chat_response(graph_state, decision, iterations, tool_observations)
            else:
                state.set_response(
                    AgentResponse(
                        message=decision.message or "已处理请求。",
                        data={
                            "intent": state.intent.intent if state.intent else None,
                            "assistant_decision": decision.type,
                            "reason": decision.reason,
                            "iterations": iterations,
                            "tool_observations": len(tool_observations),
                        },
                        followup_question=decision.message if decision.type == "ask_followup" else None,
                    )
                )
                state.status = "completed"
        elif _should_preserve_assistant_final_answer(decision=decision, is_mock=is_mock):
            _set_assistant_final_answer_response(graph_state, decision, iterations, tool_observations)
            state.status = "completed"
        elif state.status != "failed":
            state.status = "completed"

    return {
        **graph_state,
        "assistant_decision": decision,
        "assistant_iterations": iterations + 1,
    }


def _ensure_rule_plan(graph_state: AssistantLoopState) -> None:
    """Populate intent, plan, and selected tools for deterministic offline ReAct runs."""

    state = graph_state["state"]
    request = graph_state["request"]
    router = graph_state.get("router") or ToolRouter()
    if state.intent is None:
        detector = graph_state.get("intent_detector") or IntentDetector()
        state.set_intent(detector.detect(request))
    if state.plan is None:
        state.set_plan(_route_with_optional_request(router, state.intent, request))
    state.selected_tools = _select_tools_with_optional_request(router, state.intent, request)


def _max_iteration_final_answer(max_iterations: int) -> AssistantDecision:
    return AssistantDecision(
        type="final_answer",
        message=f"已达到最大工具调用次数 ({max_iterations})，这是我能提供的最好回答。",
        reason="安全限制：防止无限工具调用循环",
    )


def _request_final_answer_after_tool_limit(
    *,
    chat_adapter: ChatAdapter,
    state: AgentState,
    request: UserRequest,
    observations: list[dict[str, Any]],
    iteration: int,
    max_iterations: int,
) -> AssistantDecision:
    """Ask the real assistant to summarize instead of issuing another tool call at the limit."""

    prompt = _build_final_only_prompt(
        request=request,
        observations=observations,
        iteration=iteration,
        max_iterations=max_iterations,
    )
    result = chat_adapter.chat(
        ChatRequest(
            user_id=state.user_id,
            session_id=state.session_id,
            user_query=prompt,
            temperature=0.2,
            max_tokens=1024,
        )
    )
    decision = AssistantDecision.from_llm_output(result.response_text if result.success else "")
    if decision.type == "final_answer" and decision.message:
        return decision
    return _max_iteration_final_answer(max_iterations)


def _mock_assistant_decision_from_plan(
    *,
    request: UserRequest,
    tool_observations: list[dict[str, Any]],
    state: AgentState,
    outputs_by_step: dict[str, ToolResult],
) -> AssistantDecision:
    """Return the next deterministic ReAct decision from the rule-based plan."""

    if state.plan is None:
        return _mock_assistant_decision(request, tool_observations, [], 0, 1)

    if state.plan.requires_followup:
        return AssistantDecision(
            type="ask_followup",
            message=state.plan.followup_question or "请补充你想让我处理的对象或目标。",
            reason="计划缺少必要输入，需要追问用户。",
        )

    executable_steps = [step for step in state.plan.steps if step.tool_name is not None]
    next_index = len(state.tool_results)
    if next_index < len(executable_steps):
        step = executable_steps[next_index]
        from multimodal_agent.agent.tool_input_builder import build_tool_input

        return AssistantDecision(
            type="tool_call",
            tool_name=step.tool_name,
            tool_input=build_tool_input(step.action, request, outputs_by_step),
            reason=step.reason or f"执行计划步骤：{step.action}",
        )

    return AssistantDecision(
        type="final_answer",
        message=None,
        reason="计划步骤已执行完毕，交给响应合成器生成最终答复。",
    )


def _set_direct_chat_response(
    graph_state: AssistantLoopState,
    decision: AssistantDecision,
    iterations: int,
    tool_observations: list[dict[str, Any]],
) -> None:
    """Run direct_chat through the chat adapter so text-only ReAct has a contract."""

    state = graph_state["state"]
    request = graph_state["request"]
    memory_summaries = [item.summary for item in state.memory_context]
    memory_context_text = state.request.metadata.get("memory_context_text", "")
    result = graph_state["chat_adapter"].chat(
        build_direct_chat_request(
            request,
            memory_context=memory_summaries,
            system_instruction="You are a helpful text-first assistant.",
        )
    )
    errors = [error.model_dump(mode="json") for error in result.errors]
    contract = build_text_capability_output(
        capability="direct_chat",
        status="succeeded" if result.success else "failed",
        output_ref=result.output_ref,
        data={"response_text": result.response_text, "provider": result.provider, "model": result.model},
        errors=errors,
    )
    state.provider_budget.record_call(
        run_id=state.run_id,
        capability="direct_chat",
        provider=result.provider,
        model=result.model,
        input_size_bytes=len((request.text or "").encode("utf-8")),
        latency_ms=result.latency_ms,
        status="succeeded" if result.success else "failed",
    )
    message = result.response_text if result.success else decision.message or "已处理请求。"
    if result.success and memory_summaries:
        message = f"{message}；参考记忆：{memory_summaries[0]}"
    state.set_response(
        AgentResponse(
            message=message,
            data={
                "intent": state.intent.intent if state.intent else None,
                "assistant_decision": decision.type,
                "reason": decision.reason,
                "iterations": iterations,
                "tool_observations": len(tool_observations),
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


def _should_preserve_assistant_final_answer(*, decision: AssistantDecision, is_mock: bool) -> bool:
    """Return true when an assistant final answer should bypass response composition."""

    return decision.type == "final_answer" and bool(decision.message) and not is_mock


def _set_assistant_final_answer_response(
    graph_state: AssistantLoopState,
    decision: AssistantDecision,
    iterations: int,
    tool_observations: list[dict[str, Any]],
) -> None:
    """Persist the real assistant final answer after tool observations."""

    state = graph_state["state"]
    contracts = [
        result.contract.model_dump(mode="json")
        for result in state.tool_results
        if result.contract is not None
    ]
    output_refs = [result.output_ref for result in state.tool_results if result.output_ref]
    failures = [
        {
            "source": error.source,
            "code": error.details.get("code", "unknown_error"),
            "message": error.message,
            "recovery_action": error.details.get("recovery_action", "stop_with_error"),
            "optional_step": error.details.get("optional_step", False),
        }
        for error in state.errors
    ]
    state.set_response(
        AgentResponse(
            message=decision.message or "已处理请求。",
            data={
                "intent": state.intent.intent if state.intent else None,
                "final_answer_source": "assistant_loop",
                "assistant_decision": decision.type,
                "reason": decision.reason,
                "iterations": iterations,
                "tool_count": len(state.tool_calls),
                "tool_observations": len(tool_observations),
                "contracts": contracts,
                "output_refs": output_refs,
                "errors": failures,
                "provider_budget": state.provider_budget.summary(),
            },
            output_refs=output_refs,
        )
    )


def _is_direct_chat_state(state: AgentState) -> bool:
    return state.intent is not None and canonical_intent(state.intent.intent) == "direct_chat"


def _is_mock_chat_adapter(chat_adapter: ChatAdapter) -> bool:
    return getattr(chat_adapter, "provider", "") == "mock" or hasattr(chat_adapter, "MockChatAdapter")


def execute_requested_tool_node(graph_state: AssistantLoopState) -> AssistantLoopState:
    """
    Execute the tool requested by the assistant.

    Reads the assistant_decision, validates it, runs the tool,
    and stores the observation for the next iteration.
    """
    state = graph_state["state"]
    decision = graph_state.get("assistant_decision")
    tool_executor = graph_state["tool_executor"]
    tool_observations = graph_state.get("tool_observations", [])

    if decision is None or decision.type != "tool_call":
        return graph_state

    tool_name = decision.tool_name
    tool_input = decision.tool_input or {}
    step = _current_plan_step(state, tool_name)
    step_id = step.step_id if step is not None else f"assistant_loop_{len(tool_observations) + 1}"

    validation = ActionValidator().validate(
        decision=decision,
        registry=tool_executor.registry,
        request=graph_state["request"],
        state=state,
    )
    state.request.metadata["last_action_validator"] = validation.model_dump(mode="json")
    if not validation.accepted:
        error = AgentError(
            message=validation.message,
            source=tool_name or "assistant_loop",
            details={"code": validation.code, "recovery_action": "stop_with_error", "validator_result": validation.model_dump(mode="json")},
        )
        state.errors.append(error)
        observation = rejected_observation(
            tool_name=tool_name or "unknown",
            error_code=validation.code,
            error_message=validation.message,
        )
        _record_action_rejection(graph_state, observation, validation.model_dump(mode="json"))
        guard = LoopGuard(state.request.metadata).record_validation_rejection(validation.code, tool_name)
        if guard.triggered:
            _record_loop_guard(graph_state, guard)
            state.set_response(
                AgentResponse(
                    message=f"我没有执行这个工具调用：{validation.message}",
                    data={
                        "intent": state.intent.intent if state.intent else None,
                        "assistant_decision": "final_answer",
                        "validator_result": validation.model_dump(mode="json"),
                        "loop_guard": guard.__dict__,
                        "errors": [{"code": validation.code, "message": validation.message}],
                    },
                )
            )
        return {
            **graph_state,
            "tool_observations": _record_react_observation(graph_state, tool_observations, observation),
        }

    try:
        result = tool_executor.run_tool(
            state,
            step_id,
            tool_name,
            tool_input,
            step=step,
            trace_store=graph_state.get("trace_store"),
            trace_id=graph_state.get("trace_id"),
            node_name=graph_state.get("current_node_name", "execute_tool"),
        )

        observation = observation_from_tool_result(result)
        guard = (
            LoopGuardDecision(False, "ok", "Guard not triggered for optional step.")
            if step is not None and step.optional
            else LoopGuard(state.request.metadata).record_tool_result(tool_name=tool_name, success=result.success)
        )
        if guard.triggered:
            _record_loop_guard(graph_state, guard)
            if state.status != "failed":
                state.set_response(
                    AgentResponse(
                        message=f"{tool_name} 执行失败，我已停止重复调用并保留当前结果。",
                        data={
                            "intent": state.intent.intent if state.intent else None,
                            "assistant_decision": "final_answer",
                            "loop_guard": guard.__dict__,
                        },
                    )
                )

        return {
            **graph_state,
            "tool_observations": _record_react_observation(graph_state, tool_observations, observation),
            "outputs_by_step": {
                **graph_state["outputs_by_step"],
                step_id: result,
            },
        }
    except Exception as e:
        error = AgentError(
            message=f"工具执行异常：{str(e)}",
            source=tool_name,
            details={"tool_input": tool_input},
        )
        state.errors.append(error)
        observation = rejected_observation(
            tool_name=tool_name or "unknown",
            error_code="tool_exception",
            error_message=str(e),
        )
        return {
            **graph_state,
            "tool_observations": _record_react_observation(graph_state, tool_observations, observation),
        }


def _current_plan_step(state: AgentState, tool_name: str):
    if state.plan is None:
        return None
    executable_steps = [step for step in state.plan.steps if step.tool_name is not None]
    result_count = len(state.tool_results)
    index = min(result_count, max(len(executable_steps) - 1, 0))
    if executable_steps and executable_steps[index].tool_name == tool_name:
        return executable_steps[index]
    for step in executable_steps:
        if step.tool_name == tool_name:
            return step
    return None


def _route_with_optional_request(router: ToolRouter, intent, request: UserRequest):
    if len(signature(router.route).parameters) >= 2:
        return router.route(intent, request)
    return router.route(intent)


def _select_tools_with_optional_request(router: ToolRouter, intent, request: UserRequest):
    if len(signature(router.select_tools).parameters) >= 2:
        return router.select_tools(intent, request)
    return router.select_tools(intent)


def route_after_assistant(graph_state: AssistantLoopState) -> str:
    """
    Route after the assistant decision.

    Returns "execute_tool" or "finish".
    """
    state = graph_state["state"]
    decision = graph_state.get("assistant_decision")
    iterations = graph_state.get("assistant_iterations", 0)

    if state.status == "failed":
        return "finish"

    if state.status == "completed":
        return "finish"

    if iterations >= _get_max_tool_iterations():
        return "finish"

    if decision is None:
        return "finish"

    if decision.type == "tool_call":
        if not decision.tool_name:
            return "finish"
        return "execute_tool"

    return "finish"


def _build_assistant_prompt(
    request: UserRequest,
    memory_summaries: list[str],
    memory_text: str,
    observations: list[dict[str, Any]],
    tools_desc: list[dict[str, Any]],
    iteration: int,
) -> str:
    """Build the assistant prompt with all context."""
    user_query = request.text or ""

    prompt = f"""你是一个多模态智能助手，帮助用户处理各种任务。

当前迭代：{iteration + 1}

用户请求：{user_query}
"""

    if request.image_ids:
        prompt += f"\n附带图片 ID：{request.image_ids}"
    if request.video_ids:
        prompt += f"\n附带视频 ID：{request.video_ids}"

    conversation_context = request.metadata.get("conversation_context_text")
    if isinstance(conversation_context, str) and conversation_context.strip():
        prompt += f"\n\n多轮对话历史：\n{conversation_context.strip()}"

    if memory_summaries:
        prompt += f"\n\n相关记忆：{memory_text}"

    if observations:
        prompt += f"\n\n已执行工具和结果：{json.dumps(observations, ensure_ascii=False, indent=2)}"

    prompt += f"\n\n可用工具：{json.dumps(tools_desc, ensure_ascii=False, indent=2)}"

    prompt += """

请决定下一步操作，输出严格的 JSON 格式：

情况 1：直接回答用户
{
    "type": "final_answer",
    "message": "你的回答内容",
    "reason": "为什么可以直接回答"
}

情况 2：追问用户
{
    "type": "ask_followup",
    "message": "你的追问内容",
    "reason": "为什么需要追问"
}

情况 3：调用工具
{
    "type": "tool_call",
    "tool_name": "工具名称",
    "tool_input": {"参数名": "参数值"},
    "reason": "为什么调用这个工具"
}
"""

    return prompt


def _build_final_only_prompt(
    *,
    request: UserRequest,
    observations: list[dict[str, Any]],
    iteration: int,
    max_iterations: int,
) -> str:
    """Build a prompt that forbids more tool calls and requests a final answer."""

    user_query = request.text or ""
    conversation_context = request.metadata.get("conversation_context_text")
    history_section = ""
    if isinstance(conversation_context, str) and conversation_context.strip():
        history_section = f"\n多轮对话历史：\n{conversation_context.strip()}\n"
    return f"""你是一个多模态智能助手，正在执行 ReAct 工具调用流程。

用户请求：{user_query}
{history_section}

当前已经达到工具调用上限附近：
当前迭代：{iteration + 1}
最大工具调用次数：{max_iterations}

已执行工具和结果：
{json.dumps(observations, ensure_ascii=False, indent=2)}

不要继续调用任何工具。请基于已有 observation 给出诚实、清晰的最终回答。
如果工具结果与用户请求不匹配，请明确说明这一点，并给出你能提供的最佳建议。

必须只输出严格 JSON：
{{
    "type": "final_answer",
    "message": "你的最终回答",
    "reason": "为什么现在应该停止工具调用并回答"
}}
"""


def _get_tool_context(state: AgentState) -> Any:
    """Get tool context from agent state."""
    try:
        from multimodal_agent.tools.base import ToolContext
        return ToolContext(
            run_id=state.run_id,
            user_id=state.user_id,
            session_id=state.session_id,
        )
    except Exception:
        return None


def _record_react_decision(graph_state: AssistantLoopState, decision: AssistantDecision, iteration: int) -> None:
    """Keep compact decision trace data for local demo inspection."""

    state = graph_state["state"]
    steps = state.request.metadata.setdefault("assistant_loop_steps", [])
    if not isinstance(steps, list):
        steps = []
        state.request.metadata["assistant_loop_steps"] = steps
    trace_event = _decision_trace_event(decision, iteration)
    decision_trace = state.request.metadata.setdefault("decision_trace", [])
    if isinstance(decision_trace, list):
        decision_trace.append(trace_event)
    steps.append(
        {
            "iteration": iteration + 1,
            "decision_type": decision.type,
            "tool_name": decision.tool_name,
            "tool_input": decision.tool_input or {},
            "message": decision.message,
            "reason": decision.reason,
            "decision_summary": decision.reason,
            "confidence": decision.confidence,
            "safety_notes": decision.safety_notes,
        }
    )
    _emit_agent_trace_event(graph_state, trace_event)
    _append_trace(
        graph_state,
        event_type="assistant_decision",
        status=decision.type,
        tool_name=decision.tool_name,
        output_summary={
            "decision_type": decision.type,
            "reason": decision.reason,
            "confidence": decision.confidence,
            "message_present": bool(decision.message),
        },
    )


def _record_react_observation(
    graph_state: AssistantLoopState,
    existing: list[dict[str, Any]],
    observation: ToolObservation | dict[str, Any],
) -> list[dict[str, Any]]:
    """Append a tool observation to both graph state and demo metadata."""

    state = graph_state["state"]
    payload = observation.model_dump(mode="json") if isinstance(observation, ToolObservation) else observation
    observations = existing + [payload]
    trace_event = _observation_trace_event(payload, len(observations))
    decision_trace = state.request.metadata.setdefault("decision_trace", [])
    if isinstance(decision_trace, list):
        decision_trace.append(trace_event)
    steps = state.request.metadata.setdefault("assistant_loop_steps", [])
    if isinstance(steps, list):
        steps.append(
            {
                "iteration": len(observations),
                "observation_tool": payload.get("tool_name"),
                "status": payload.get("status"),
                "success": payload.get("status") == "succeeded",
                "summary": payload.get("summary"),
                "output_ref": payload.get("output_ref"),
                "error_code": payload.get("error_code"),
                "error": payload.get("error_message"),
                "next_step_hint": payload.get("next_step_hint"),
                "recovery_hint": payload.get("next_step_hint"),
            }
        )
    _emit_agent_trace_event(graph_state, trace_event)
    _append_trace(
        graph_state,
        event_type="tool_observation",
        status=payload.get("status"),
        tool_name=payload.get("tool_name"),
        output_summary={
            "summary": payload.get("summary"),
            "output_ref": payload.get("output_ref"),
            "next_step_hint": payload.get("next_step_hint"),
        },
        error={"code": payload.get("error_code"), "message": payload.get("error_message")} if payload.get("error_code") else None,
    )
    return observations


def _decision_trace_event(decision: AssistantDecision, iteration: int) -> dict[str, Any]:
    event_name = "final_answer" if decision.type == "final_answer" else "decision"
    payload: dict[str, Any] = {
        "iteration": iteration + 1,
        "event": event_name,
        "decision_type": "clarification" if decision.type == "ask_followup" else decision.type,
        "decision_summary": decision.reason or "",
    }
    if decision.type == "tool_call":
        payload["action"] = decision.tool_name
        payload["action_input"] = decision.tool_input or {}
    if decision.type == "final_answer":
        payload["answer"] = decision.message or ""
    return payload


def _observation_trace_event(payload: dict[str, Any], iteration: int) -> dict[str, Any]:
    event: dict[str, Any] = {
        "iteration": iteration,
        "event": "observation",
        "action": payload.get("tool_name") or "unknown",
        "success": payload.get("status") == "succeeded",
        "output_ref": payload.get("output_ref"),
        "output_preview": payload.get("summary"),
        "recovery_hint": payload.get("next_step_hint"),
    }
    if payload.get("error_message") or payload.get("error_code"):
        event["error"] = {
            "code": payload.get("error_code"),
            "message": payload.get("error_message") or "Tool failed.",
            "retryable": False,
        }
    return {key: value for key, value in event.items() if value is not None}


def _emit_agent_trace_event(graph_state: AssistantLoopState, trace_event: dict[str, Any]) -> None:
    tool_executor = graph_state.get("tool_executor")
    event_sink = getattr(tool_executor, "event_sink", None)
    if event_sink is None:
        return
    state = graph_state["state"]
    event_type = {
        "decision": "agent_trace_decision",
        "observation": "agent_trace_observation",
        "final_answer": "agent_trace_final_answer",
    }.get(str(trace_event.get("event")), "agent_trace_decision")
    event_sink.emit(
        AgentEvent(
            type=cast(Any, event_type),
            session_id=state.session_id,
            run_id=state.run_id,
            tool_name=trace_event.get("action") if isinstance(trace_event.get("action"), str) else None,
            output_ref=trace_event.get("output_ref") if isinstance(trace_event.get("output_ref"), str) else None,
            text=trace_event.get("answer") if isinstance(trace_event.get("answer"), str) else None,
            error=trace_event.get("error"),
            payload={"decision_trace": trace_event},
        )
    )


def _guard_final_answer(guard: LoopGuardDecision) -> AssistantDecision:
    return AssistantDecision(
        type="final_answer",
        message="工具调用保护已触发，我已停止继续调用工具。请补充更明确的信息，或稍后重试。",
        reason=guard.message,
        safety_notes=[guard.code],
    )


def _record_action_rejection(
    graph_state: AssistantLoopState,
    observation: ToolObservation,
    validator_result: dict[str, Any],
) -> None:
    _append_trace(
        graph_state,
        event_type="action_rejected",
        status="rejected",
        tool_name=observation.tool_name,
        output_summary={"validator_result": validator_result, "observation_summary": observation.summary},
        error={"code": observation.error_code, "message": observation.error_message},
    )


def _record_loop_guard(graph_state: AssistantLoopState, guard: LoopGuardDecision) -> None:
    _append_trace(
        graph_state,
        event_type="loop_guard_triggered",
        status="triggered",
        error={"code": guard.code, "message": guard.message},
    )


def _append_trace(
    graph_state: AssistantLoopState,
    *,
    event_type: str,
    status: str | None = None,
    tool_name: str | None = None,
    output_summary: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> None:
    trace_store = graph_state.get("trace_store")
    trace_id = graph_state.get("trace_id")
    state = graph_state["state"]
    if trace_store is None or trace_id is None:
        return
    trace_store.append(
        TraceEvent(
            trace_id=trace_id,
            run_id=state.run_id,
            node_name=graph_state.get("current_node_name", "assistant_loop"),
            event_type=cast(Any, event_type),
            tool_name=tool_name,
            status=status,
            output_summary=output_summary or {},
            error={
                "code": error.get("code"),
                "message": sanitize_trace_value(str(error.get("message", ""))),
            }
            if error
            else None,
        )
    )


def _get_max_tool_iterations() -> int:
    """Get the maximum number of tool iterations from config or default."""
    import os
    try:
        from multimodal_agent.config import ProviderConfig
        config = ProviderConfig.from_env()
        if hasattr(config, "max_tool_iterations"):
            return config.max_tool_iterations
    except Exception:
        pass
    try:
        return int(os.environ.get("MAX_TOOL_ITERATIONS", "5"))
    except ValueError:
        return 5
