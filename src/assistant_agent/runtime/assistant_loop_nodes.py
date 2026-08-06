"""Controlled assistant loop nodes.

In real chat-adapter mode, the LLM uses provider-native responses: natural
language content for direct answers, or native tool_calls for tool requests. In
mock mode, the rule plan provides deterministic decisions for stable offline
tests.

Local code owns the minimum required guardrails around those decisions:
tool listing, native tool-call normalization, validation, execution, loop
limits, trace recording, and state mutation.
"""

from inspect import signature
from time import perf_counter
from typing import Any, NotRequired, TypedDict, cast

from assistant_agent.runtime.action_validator import ActionValidator
from assistant_agent.runtime.cancellation import AgentRunCancelled
from assistant_agent.runtime.intent import IntentDetector
from assistant_agent.runtime.llm_event_mapping import stream_delta_to_agent_event
from assistant_agent.runtime.loop_guard import LoopGuard, LoopGuardDecision
from assistant_agent.runtime.prompt_builder import build_direct_chat_request, build_text_capability_output
from assistant_agent.runtime.router import ToolRouter
from assistant_agent.runtime.run_phase import RunPhase
from assistant_agent.runtime.state import AgentError, AgentState
from assistant_agent.runtime.tool_executor import ToolExecutor
from assistant_agent.runtime.output_models import (
    AssistantTextOutput,
    AssistantToolCall,
    AssistantTurnOutput,
)
from assistant_agent.runtime.capability_models import canonical_intent
from assistant_agent.context.service import (
    AssistantDecisionContext,
    ContextPreflightFailure,
    ContextService,
)
from assistant_agent.context.finalization import finalize_fallback_text
from assistant_agent.runtime.events import AgentEvent
from assistant_agent.runtime.event_publisher import (
    AssistantStepFact,
    RuntimeEventPublisher,
)
from assistant_agent.runtime.planning_models import TaskPlan, TaskStep
from assistant_agent.runtime.requests import AgentResponse, UserRequest
from assistant_agent.tools.observation import (
    PROVIDER_TOOL_CALL_ID_KEY,
    ToolObservation,
    observation_from_tool_result,
    rejected_observation,
)
from assistant_agent.tools.observation_safety import sanitize_tool_observation_detail
from assistant_agent.tools.models import ToolResult, ToolSpec
from assistant_agent.runtime.chat_adapter import (
    ChatAdapter,
    ChatRequest,
    ChatResult,
    chat_result_kind,
)
from assistant_agent.context.token_budget import normalize_provider_token_usage
from assistant_agent.observability.trace_store import (
    TraceEvent,
    TraceObservationScope,
    TraceObservationType,
    append_observability_event,
    new_span_id,
    sanitize_trace_value,
)


PROVIDER_CONTEXT_OVERFLOW_CODES = {
    "provider_context_overflow",
    "context_length_exceeded",
    "context_overflow",
    "input_too_large",
    "provider_request_too_large",
}
MAIN_LLM_NO_ANSWER_MESSAGES = {
    "provider_timeout": "抱歉，刚才主模型没有及时响应，请再说一遍。",
    "provider_empty_response": "抱歉，刚才主模型返回为空，请再说一遍。",
}


class AssistantLoopState(TypedDict):
    """State for the assistant loop graph."""

    request: UserRequest
    state: AgentState
    intent_detector: NotRequired[IntentDetector]
    router: NotRequired[ToolRouter]
    tool_executor: NotRequired[ToolExecutor]
    chat_adapter: NotRequired[ChatAdapter]
    chat_turn: NotRequired[Any]
    context_service: NotRequired[ContextService]
    context_projector: NotRequired[Any]
    outputs_by_step: dict[str, ToolResult]
    current_step_index: int
    trace_id: NotRequired[str]
    trace_store: NotRequired[Any]
    event_sink: NotRequired[Any]
    assistant_output: NotRequired[AssistantTurnOutput | None]
    pending_tool_calls: NotRequired[list[AssistantToolCall]]
    assistant_iterations: NotRequired[int]
    tool_calls_used: NotRequired[int]
    run_phase: NotRequired[RunPhase]
    tool_observations: NotRequired[list[dict[str, Any]]]
    current_node_name: NotRequired[str]
    max_tool_iterations: NotRequired[int]
    max_plan_steps: NotRequired[int]
    max_plan_revisions: NotRequired[int]
    last_llm_span_id: NotRequired[str]
    last_llm_attempt_kind: NotRequired[str]
    response_stream_current_call_emitted: NotRequired[bool]
    response_stream_ends_with_newline: NotRequired[bool]
    response_stream_separator_pending: NotRequired[bool]


def _run_phase(graph_state: AssistantLoopState) -> RunPhase:
    value = graph_state.get("run_phase", RunPhase.ACT)
    try:
        return RunPhase(value)
    except ValueError:
        return RunPhase.ACT


def _enter_finalize_phase(
    graph_state: AssistantLoopState,
    *,
    reason: str,
    source: str,
) -> None:
    previous_phase = _run_phase(graph_state)
    if previous_phase is RunPhase.FINALIZE:
        return
    graph_state["run_phase"] = RunPhase.FINALIZE
    metadata = graph_state["state"].request.metadata
    metadata["assistant_run_phase"] = RunPhase.FINALIZE.value
    metadata.setdefault("assistant_finalize_reason", reason)
    _append_trace(
        graph_state,
        event_type="observability",
        canonical_event="runtime.phase.changed",
        observation_type="event",
        observation_scope="runtime",
        status="transitioned",
        attributes={
            "from_phase": previous_phase.value,
            "to_phase": RunPhase.FINALIZE.value,
            "reason": reason,
            "source": source,
        },
    )


def assistant_node(graph_state: AssistantLoopState) -> AssistantLoopState:
    """
    Central assistant decision node.

    Reads the request, memory, tool observations, and decides the next action.
    """
    state = graph_state["state"]
    chat_adapter = graph_state["chat_adapter"]
    iterations = graph_state.get("assistant_iterations", 0)
    tool_observations = graph_state.get("tool_observations", [])
    max_iterations = int(graph_state.get("max_tool_iterations", _get_max_tool_iterations()))

    if state.status == "completed" and state.response is not None:
        decision = AssistantTextOutput(
            text=state.response.message,
            reason="Run already completed before the next assistant turn.",
        )
        _record_react_decision(graph_state, decision, iterations)
        return {
            **graph_state,
            "assistant_output": decision,
            "assistant_iterations": iterations + 1,
        }

    request = graph_state["request"]
    is_mock = _is_mock_chat_adapter(chat_adapter)

    if is_mock:
        _ensure_rule_plan(graph_state)

    context = _build_decision_context(
        graph_state,
        request=request,
        tool_observations=tool_observations,
        iterations=iterations,
        max_iterations=max_iterations,
        is_mock=is_mock,
    )
    decision, context = _decide_next_action(
        graph_state,
        context=context,
        chat_adapter=chat_adapter,
        state=state,
    )
    decision = _apply_decision_guards(graph_state, decision, context)
    pending_decisions = graph_state.get("pending_tool_calls")
    if isinstance(pending_decisions, list) and pending_decisions:
        if isinstance(decision, AssistantToolCall):
            pending_decisions[0] = decision
        else:
            graph_state["pending_tool_calls"] = []
    _record_react_decision(graph_state, decision, iterations, context=context)
    _apply_terminal_decision(graph_state, decision, context)

    return _assistant_node_result(graph_state, decision, iterations)


def _assistant_node_result(
    graph_state: AssistantLoopState,
    decision: AssistantTurnOutput,
    iterations: int,
) -> AssistantLoopState:
    return {
        **graph_state,
        "assistant_output": decision,
        "assistant_iterations": iterations + 1,
    }


def _build_decision_context(
    graph_state: AssistantLoopState,
    *,
    request: UserRequest,
    tool_observations: list[dict[str, Any]],
    iterations: int,
    max_iterations: int,
    is_mock: bool,
) -> AssistantDecisionContext:
    tool_calls_used = int(graph_state.get("tool_calls_used", len(tool_observations)))
    if tool_calls_used >= max_iterations:
        _enter_finalize_phase(
            graph_state,
            reason="tool_call_budget_exhausted",
            source="tool_budget",
        )
    run_phase = _run_phase(graph_state)
    if run_phase is RunPhase.FINALIZE:
        tool_specs = []
        if tool_calls_used >= max_iterations:
            request.metadata["tool_call_budget_exhausted"] = True
    else:
        try:
            tool_specs = _list_tool_specs(graph_state["tool_executor"].registry)
        except Exception as exc:
            tool_specs = []
            _record_tool_description_error(graph_state, exc)
    context_service = _context_service(graph_state)
    return context_service.build(
        state=graph_state["state"],
        request=request,
        observations=tool_observations,
        tool_specs=tool_specs,
        iteration=iterations,
        max_iterations=max_iterations,
        is_mock=is_mock,
        answer_only=run_phase is RunPhase.FINALIZE,
        trace_store=graph_state.get("trace_store"),
        trace_id=graph_state.get("trace_id"),
        node_name=graph_state.get("current_node_name", "assistant"),
        registry_generation=getattr(
            graph_state["tool_executor"].registry,
            "generation",
            None,
        ),
        context_projector=graph_state.get("context_projector"),
        native_calls=_native_tool_calls_from_metadata(graph_state["state"]),
    )


def _context_service(graph_state: AssistantLoopState) -> ContextService:
    service = graph_state.get("context_service")
    if service is None:
        raise RuntimeError("assistant loop requires ContextService")
    return service


def _list_tool_specs(registry: Any) -> list[ToolSpec]:
    """Read ToolSpec contracts, falling back to legacy descriptions when needed."""

    if hasattr(registry, "list_specs"):
        specs = registry.list_specs()
        return [spec if isinstance(spec, ToolSpec) else ToolSpec.model_validate(spec) for spec in specs]
    descriptions = registry.describe_tools()
    return [ToolSpec.model_validate(item) for item in descriptions]


def _decide_next_action(
    graph_state: AssistantLoopState,
    *,
    context: AssistantDecisionContext,
    chat_adapter: ChatAdapter,
    state: AgentState,
) -> tuple[AssistantTurnOutput, AssistantDecisionContext]:
    """Select the next assistant action without mutating response state."""

    if context.is_mock:
        return _decide_with_mock_plan(graph_state, context, state), context
    return _decide_with_llm(chat_adapter, context, state, graph_state=graph_state)


def _decide_with_mock_plan(
    graph_state: AssistantLoopState,
    context: AssistantDecisionContext,
    state: AgentState,
) -> AssistantTurnOutput:
    """Return deterministic offline decisions backed by the rule-generated plan."""

    decision = _mock_assistant_decision_from_plan(
        request=context.request,
        tool_observations=context.tool_observations,
        state=state,
        outputs_by_step=graph_state["outputs_by_step"],
    )
    return decision


def _decide_with_llm(
    chat_adapter: ChatAdapter,
    context: AssistantDecisionContext,
    state: AgentState,
    *,
    graph_state: AssistantLoopState,
) -> tuple[AssistantTurnOutput, AssistantDecisionContext]:
    """Ask the real chat adapter for the next ReAct action."""

    use_native_tools = _use_native_tool_calling(context, chat_adapter)
    if not use_native_tools:
        return (
            AssistantTextOutput(
                text="当前模型不支持原生工具调用，无法执行 agent runtime。",
                reason="Provider-native tool calling is required; legacy JSON controller is disabled.",
                safety_notes=["native_tool_calling_unsupported"],
            ),
            context,
        )
    context_service = _context_service(graph_state)
    preflight = context_service.preflight(
        context,
        state,
        trace_store=graph_state.get("trace_store"),
        trace_id=graph_state.get("trace_id"),
        node_name=graph_state.get("current_node_name", "assistant"),
        context_projector=graph_state.get("context_projector"),
    )
    context = preflight.context
    graph_state["request"] = context.request
    if preflight.failure is not None:
        return _context_compaction_failed_output(preflight.failure), context
    answer_only_base_request = preflight.request
    request = _with_response_stream_callback(
        preflight.request,
        graph_state,
        source="assistant_langgraph_answer",
    )
    result = _run_chat_turn(
        graph_state,
        chat_adapter,
        request,
        attempt_kind="finalize" if context.answer_only else "primary",
    )
    _record_chat_usage_metadata(state, result)
    if (
        _is_provider_context_overflow_result(result)
        and context_service.compactor is not None
        and _can_retry_provider_context_overflow(state)
    ):
        _record_provider_context_overflow(state, result)
        retry_preflight = context_service.preflight(
            context,
            state,
            force_hard=True,
            trace_store=graph_state.get("trace_store"),
            trace_id=graph_state.get("trace_id"),
            node_name=graph_state.get("current_node_name", "assistant"),
            context_projector=graph_state.get("context_projector"),
        )
        graph_state["request"] = retry_preflight.context.request
        if retry_preflight.failure is not None:
            return (
                _context_compaction_failed_output(retry_preflight.failure),
                retry_preflight.context,
            )
        retry_context = retry_preflight.context
        answer_only_base_request = retry_preflight.request
        retry_request = _with_response_stream_callback(
            retry_preflight.request,
            graph_state,
            source="assistant_langgraph_answer",
        )
        result = _run_chat_turn(
            graph_state,
            chat_adapter,
            retry_request,
            attempt_kind="context_overflow_retry",
        )
        _record_chat_usage_metadata(state, result)
        context = retry_context
        if _is_provider_context_overflow_result(result):
            _record_provider_context_overflow(state, result, retry_failed=True)
            return _provider_context_overflow_final_answer(result), context
    if result.success and result.tool_calls:
        _mark_response_stream_tool_boundary(graph_state)
        if not context_service.selected_tool_specs(context):
            state.request.metadata["tool_call_returned_after_budget_exhaustion"] = True
            state.request.metadata["finalization_protocol_violation"] = True
            state.request.metadata["finalization_protocol_violation_count"] = 1
            retry_request = _with_response_stream_callback(
                _with_finalize_protocol_retry_guidance(answer_only_base_request),
                graph_state,
                source="assistant_langgraph_answer",
            )
            result = _run_chat_turn(
                graph_state,
                chat_adapter,
                retry_request,
                attempt_kind="finalize_protocol_retry",
            )
            _record_chat_usage_metadata(state, result)
            if (
                result.success
                and not result.tool_calls
                and chat_result_kind(result) in {"text", "refusal"}
            ):
                decision = _native_final_decision(result)
                graph_state["pending_tool_calls"] = []
                return decision, context
            state.request.metadata["answer_only_retry_failed"] = True
            state.request.metadata["finalization_protocol_violation_count"] = 2
            return _finalize_fallback(context), context
        pending_decisions = _native_tool_call_decisions(result)
        _record_native_tool_calls(
            state,
            result,
            iteration=context.iterations,
        )
        graph_state["pending_tool_calls"] = pending_decisions
        decision = pending_decisions[0]
    elif (
        context.answer_only
        and chat_result_kind(result) in {"error", "truncated", "empty"}
    ):
        graph_state["pending_tool_calls"] = []
        state.request.metadata["finalization_synthesis_failed"] = True
        decision = _finalize_fallback(context)
    elif result.success:
        graph_state["pending_tool_calls"] = []
        decision = _native_final_decision(result)
    else:
        graph_state["pending_tool_calls"] = []
        decision = _native_final_decision(result)
    return decision, context


def _context_compaction_failed_output(
    failure: ContextPreflightFailure,
) -> AssistantTextOutput:
    return AssistantTextOutput(
        text="当前会话上下文过长且压缩失败，请新建会话或缩短输入后重试。",
        reason=failure.reason,
        safety_notes=["context_compaction_failed"],
    )


def _run_chat_turn(
    graph_state: AssistantLoopState,
    chat_adapter: ChatAdapter,
    request: ChatRequest,
    *,
    attempt_kind: str,
) -> ChatResult:
    state = graph_state["state"]
    iteration = int(graph_state.get("assistant_iterations", 0)) + 1
    span_id = new_span_id()
    graph_state["last_llm_span_id"] = span_id
    graph_state["last_llm_attempt_kind"] = attempt_kind
    started_at = perf_counter()
    provider = _safe_provider_label(getattr(chat_adapter, "provider", None))
    model = _safe_provider_label(getattr(chat_adapter, "model", None))
    _record_local_llm_input(
        state,
        trace_id=graph_state.get("trace_id") or state.trace_id,
        iteration=iteration,
        provider=provider,
        model=model,
        request=request,
        span_id=span_id,
        attempt_kind=attempt_kind,
    )
    prior_provider_request_callback = request.provider_request_callback

    def record_provider_request(payload: dict[str, Any]) -> None:
        _record_local_llm_input(
            state,
            trace_id=graph_state.get("trace_id") or state.trace_id,
            iteration=iteration,
            provider=provider,
            model=model,
            request=request,
            span_id=span_id,
            attempt_kind=attempt_kind,
            provider_payload=payload,
        )
        if prior_provider_request_callback is not None:
            prior_provider_request_callback(payload)

    request = request.model_copy(
        update={"provider_request_callback": record_provider_request}
    )
    append_observability_event(
        graph_state.get("trace_store"),
        trace_id=graph_state.get("trace_id") or state.trace_id,
        run_id=state.run_id,
        user_id=state.user_id,
        session_id=state.session_id,
        canonical_event="llm.chat.started",
        node_name=graph_state.get("current_node_name", "assistant_loop"),
        status="started",
        provider=provider,
        model=model,
        span_id=span_id,
        attributes={
            "iteration": iteration,
            "max_iterations": int(graph_state.get("max_tool_iterations", _get_max_tool_iterations())),
            "tool_spec_count": len(request.tools),
            "attempt_kind": attempt_kind,
            "run_phase": _run_phase(graph_state).value,
        },
    )
    runner = graph_state.get("chat_turn")
    try:
        result = cast(ChatResult, runner(request)) if callable(runner) else chat_adapter.chat(request)
    except Exception as exc:
        wall_latency_ms = _elapsed_ms(started_at)
        append_observability_event(
            graph_state.get("trace_store"),
            trace_id=graph_state.get("trace_id") or state.trace_id,
            run_id=state.run_id,
            user_id=state.user_id,
            session_id=state.session_id,
            canonical_event="llm.chat.finished",
            observation_type="generation",
            observation_scope="iteration",
            node_name=graph_state.get("current_node_name", "assistant_loop"),
            status="failed",
            provider=provider,
            model=model,
            latency_ms=wall_latency_ms,
            span_id=span_id,
            attributes={
                "iteration": iteration,
                "wall_latency_ms": wall_latency_ms,
                "attempt_kind": attempt_kind,
                "run_phase": _run_phase(graph_state).value,
            },
            error={"code": "provider_call_failed", "message": sanitize_trace_value(str(exc))},
        )
        raise
    _record_local_llm_output(
        state,
        trace_id=graph_state.get("trace_id") or state.trace_id,
        iteration=iteration,
        span_id=span_id,
        attempt_kind=attempt_kind,
        result=result,
    )
    wall_latency_ms = _elapsed_ms(started_at)
    provider_latency_ms = result.latency_ms
    runtime_route = _runtime_route_for_chat_result(result)
    append_observability_event(
        graph_state.get("trace_store"),
        trace_id=graph_state.get("trace_id") or state.trace_id,
        run_id=state.run_id,
        user_id=state.user_id,
        session_id=state.session_id,
        canonical_event="llm.chat.finished",
        observation_type="generation",
        observation_scope="iteration",
        node_name=graph_state.get("current_node_name", "assistant_loop"),
        status="succeeded" if result.success else "failed",
        provider=result.provider,
        model=result.model,
        latency_ms=provider_latency_ms if provider_latency_ms is not None else wall_latency_ms,
        span_id=span_id,
        attributes={
            "iteration": iteration,
            "result_kind": chat_result_kind(result),
            "runtime_route": runtime_route,
            "route_branch": runtime_route["selected_branch"],
            "runtime_action": runtime_route["runtime_action"],
            "transport_mode": (
                result.protocol_response.transport_mode
                if result.protocol_response is not None
                else "unknown"
            ),
            "token_delta_count": (
                result.protocol_response.token_delta_count
                if result.protocol_response is not None
                else 0
            ),
            "tool_call_delta_count": (
                result.protocol_response.tool_call_delta_count
                if result.protocol_response is not None
                else 0
            ),
            "reasoning_delta_count": (
                result.protocol_response.reasoning_delta_count
                if result.protocol_response is not None
                else 0
            ),
            "terminal_seen": (
                result.protocol_response.terminal_seen
                if result.protocol_response is not None
                else None
            ),
            "finish_reason": result.finish_reason,
            "tool_call_count": len(result.tool_calls),
            "provider_latency_ms": provider_latency_ms,
            "wall_latency_ms": wall_latency_ms,
            "usage": normalize_provider_token_usage(result.usage),
            "attempt_kind": attempt_kind,
            "run_phase": _run_phase(graph_state).value,
        },
        error=_chat_result_trace_error(result),
    )
    return result


def _record_local_llm_input(
    state: AgentState,
    *,
    trace_id: str,
    iteration: int,
    provider: str | None,
    model: str | None,
    request: ChatRequest,
    span_id: str,
    attempt_kind: str,
    provider_payload: dict[str, Any] | None = None,
) -> None:
    """Capture one Provider request for local Langfuse generation input."""

    from assistant_agent.observability.trace_conversation import (
        TraceLlmInput,
        get_default_trace_conversation_store,
    )

    payload = provider_payload or _fallback_provider_request_payload(
        request,
        model=model,
    )
    get_default_trace_conversation_store().append_llm_input(
        user_id=state.user_id,
        session_id=state.session_id,
        trace_id=trace_id,
        llm_input=TraceLlmInput(
            iteration=iteration,
            span_id=span_id,
            attempt_kind=attempt_kind,
            provider=provider,
            model=model,
            request=payload,
        ),
    )


def _fallback_provider_request_payload(
    request: ChatRequest,
    *,
    model: str | None,
) -> dict[str, Any]:
    """Provide a semantic fallback for adapters without request capture support."""

    return {
        "model": model,
        "messages": request.messages,
        "tools": request.tools,
        "tool_choice": request.tool_choice,
        "response_format": request.response_format,
        "temperature": request.temperature,
        "max_tokens": request.max_tokens,
    }


def _record_local_llm_output(
    state: AgentState,
    *,
    trace_id: str,
    iteration: int,
    span_id: str,
    attempt_kind: str,
    result: ChatResult,
) -> None:
    """Capture the normalized Provider result before final-answer validation."""

    from assistant_agent.observability.trace_content_policy import (
        local_provider_protocol_capture_enabled,
        local_trace_content_enabled,
    )

    if not local_trace_content_enabled():
        return
    from assistant_agent.observability.trace_conversation import (
        TraceLlmOutput,
        get_default_trace_conversation_store,
    )

    normalized_result = cast(
        dict[str, Any],
        _sanitize_local_llm_value(
            result.model_dump(
                mode="json",
                exclude={"reasoning_content", "protocol_response"},
            )
        ),
    )
    protocol_response = None
    if result.protocol_response is not None and local_provider_protocol_capture_enabled():
        protocol_response = cast(
            dict[str, Any],
            _sanitize_local_llm_value(result.protocol_response.model_dump(mode="json")),
        )

    get_default_trace_conversation_store().append_llm_output(
        user_id=state.user_id,
        session_id=state.session_id,
        trace_id=trace_id,
        llm_output=TraceLlmOutput(
            iteration=iteration,
            span_id=span_id,
            attempt_kind=attempt_kind,
            provider=result.provider,
            model=result.model,
            normalized_result=normalized_result,
            provider_protocol_response=protocol_response,
        ),
    )


def _runtime_route_for_chat_result(result: ChatResult) -> dict[str, Any]:
    """Describe the exact runtime branch selected from normalized Provider output."""

    result_kind = chat_result_kind(result)
    selected_branch, runtime_action = {
        "error": ("provider_error", "fallback"),
        "tool_call": ("native_tool_calls", "tool_governance"),
        "refusal": ("provider_refusal", "text"),
        "truncated": ("truncated_content", "fallback"),
        "text": ("provider_content", "text"),
        "empty": ("empty_content", "fallback"),
    }[result_kind]
    return {
        "schema_version": "runtime_route_v1",
        "result_kind": result_kind,
        "selected_branch": selected_branch,
        "runtime_action": runtime_action,
        "tool_call_count": len(result.tool_calls),
    }


def _sanitize_local_llm_value(value: Any, *, key: str | None = None) -> Any:
    if key in {"reasoning_content", "assistant_reasoning_content"}:
        return "[redacted]"
    if isinstance(value, dict):
        return {
            str(item_key): _sanitize_local_llm_value(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_local_llm_value(item) for item in value]
    if isinstance(value, str):
        return "\n".join(
            line if not line.strip() else sanitize_trace_value(line)
            for line in value.split("\n")
        )
    return value


def _elapsed_ms(started_at: float) -> int:
    return max(0, int((perf_counter() - started_at) * 1000))


def _safe_provider_label(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned[:128] or None


def _chat_result_trace_error(result: ChatResult) -> dict[str, Any] | None:
    if not result.errors:
        return None
    error = result.errors[0]
    return {"code": error.code, "message": sanitize_trace_value(error.message)}


def _record_chat_usage_metadata(state: AgentState, result: ChatResult) -> None:
    usage = normalize_provider_token_usage(result.usage)
    if not usage:
        return
    metadata = state.request.metadata
    metadata["context_token_usage"] = dict(usage)
    metadata["provider_token_usage"] = dict(usage)
    metadata["last_chat_usage"] = dict(usage)
    preflight = metadata.get("context_token_preflight")
    prompt_tokens = usage.get("prompt_tokens", 0)
    if isinstance(preflight, dict) and prompt_tokens > 0:
        estimated = preflight.get("input_tokens")
        if isinstance(estimated, int) and estimated >= 0:
            preflight["provider_prompt_tokens"] = prompt_tokens
            preflight["estimation_error_tokens"] = prompt_tokens - estimated
            preflight["estimation_error_ratio"] = (
                (prompt_tokens - estimated) / prompt_tokens
            )
    history = metadata.setdefault("provider_token_usage_history", [])
    if isinstance(history, list):
        history.append(dict(usage))
        del history[:-10]


def _is_provider_context_overflow_result(result: ChatResult) -> bool:
    return any(error.code in PROVIDER_CONTEXT_OVERFLOW_CODES for error in result.errors)


def _can_retry_provider_context_overflow(state: AgentState) -> bool:
    value = state.request.metadata.get("provider_context_overflow_retry_count")
    retry_count = value if isinstance(value, int) and value >= 0 else 0
    return retry_count < 1


def _record_provider_context_overflow(
    state: AgentState,
    result: ChatResult,
    *,
    retry_failed: bool = False,
) -> None:
    metadata = state.request.metadata
    metadata["provider_context_overflow"] = True
    metadata["last_provider_error_code"] = "provider_context_overflow"
    if not retry_failed:
        metadata["provider_context_overflow_retry_count"] = _metadata_int(metadata, "provider_context_overflow_retry_count") + 1
        metadata["provider_context_overflow_retry_attempted"] = True
    else:
        metadata["provider_context_overflow_retry_failed"] = True
    provider_errors = metadata.setdefault("provider_errors", [])
    if isinstance(provider_errors, list):
        for error in result.errors:
            if error.code not in PROVIDER_CONTEXT_OVERFLOW_CODES:
                continue
            provider_errors.append(
                {
                    "code": "provider_context_overflow",
                    "message": "provider context overflow",
                    "recoverable": error.recoverable,
                    "provider": result.provider,
                    "model": result.model,
                }
            )


def _provider_context_overflow_final_answer(result: ChatResult) -> AssistantTextOutput:
    message = "模型上下文超出限制，已尝试压缩后重试一次但仍失败。请缩短输入或拆分任务后再试。"
    reason = "Provider context overflow persisted after one compaction retry."
    error_message = next((sanitize_trace_value(error.message) for error in result.errors), "")
    notes = ["provider_context_overflow", "provider_context_overflow_retry_failed"]
    if error_message:
        notes.append("provider_error_sanitized")
    return AssistantTextOutput(
        text=message,
        reason=reason,
        safety_notes=notes,
    )


def _use_native_tool_calling(context: AssistantDecisionContext, chat_adapter: ChatAdapter) -> bool:
    if context.is_mock:
        return False
    return _chat_adapter_supports_native_tools(chat_adapter)


def _chat_adapter_supports_native_tools(chat_adapter: ChatAdapter) -> bool:
    capabilities = getattr(chat_adapter, "capabilities", None)
    if capabilities is None:
        return True
    return bool(getattr(capabilities, "supports_native_tools", True))


def _native_final_decision(result: ChatResult) -> AssistantTextOutput:
    """Convert a native provider non-tool result into strict text output."""

    no_answer_code = next(
        (error.code for error in result.errors if error.code in MAIN_LLM_NO_ANSWER_MESSAGES),
        None,
    )
    if no_answer_code is not None:
        return AssistantTextOutput(
            text=MAIN_LLM_NO_ANSWER_MESSAGES[no_answer_code],
            reason=f"Main LLM returned {no_answer_code} without usable content.",
            safety_notes=[no_answer_code],
        )
    if result.errors:
        return AssistantTextOutput(
            text="抱歉，刚才模型调用失败，请再试一次。",
            reason="Main LLM returned an error without a usable terminal response.",
            safety_notes=["provider_error"],
        )
    if result.refusal:
        return AssistantTextOutput(
            text=result.refusal,
            reason=_native_finish_reason(result, fallback="Provider returned a refusal instead of a tool call."),
            safety_notes=["provider_refusal"],
        )
    if result.finish_reason == "length":
        return AssistantTextOutput(
            text="抱歉，刚才模型的回答被截断了，请缩短问题或让我分段回答。",
            reason="Provider response was truncated before completion. finish_reason=length.",
            safety_notes=["provider_response_truncated"],
        )
    if result.response_text.strip():
        return AssistantTextOutput(
            text=result.response_text.strip(),
            reason=_native_finish_reason(
                result,
                fallback="Provider finished without requesting a tool; content is the final answer.",
            ),
        )
    return AssistantTextOutput(
        text="模型没有返回可用回答。",
        reason=_native_finish_reason(result, fallback="Provider finished without content or tool calls."),
        safety_notes=["empty_native_final_answer"],
    )


def _native_tool_call_decisions(result: ChatResult) -> list[AssistantToolCall]:
    """Convert native calls without exposing same-turn model narration."""

    return [call.to_assistant_tool_call() for call in result.tool_calls]


def _native_finish_reason(result: ChatResult, *, fallback: str) -> str:
    if result.finish_reason:
        return f"{fallback} finish_reason={result.finish_reason}."
    return fallback


def _record_native_tool_calls(
    state: AgentState,
    result: ChatResult,
    *,
    iteration: int,
) -> None:
    calls = state.request.metadata.setdefault("native_tool_calls", [])
    if not isinstance(calls, list):
        return
    turn_id = f"assistant_loop_turn_{iteration + 1}"
    for call in result.tool_calls:
        payload = call.model_dump(mode="json")
        payload["assistant_turn_id"] = turn_id
        if result.reasoning_content:
            payload["assistant_reasoning_content"] = result.reasoning_content
        calls.append(payload)


def _native_tool_calls_from_metadata(state: AgentState) -> list[dict[str, Any]]:
    calls = state.request.metadata.get("native_tool_calls", [])
    return [call for call in calls if isinstance(call, dict)] if isinstance(calls, list) else []


def _apply_decision_guards(
    graph_state: AssistantLoopState,
    decision: AssistantTurnOutput,
    context: AssistantDecisionContext,
) -> AssistantTurnOutput:
    """Apply loop/safety guards after a policy proposes an assistant decision."""

    state = graph_state["state"]
    if decision.reason == "Empty or whitespace-only output.":
        guard = LoopGuard(state.request.metadata).record_empty_decision()
        if guard.triggered:
            _record_loop_guard(graph_state, guard)
            return _guard_final_answer(guard)
    if (
        isinstance(decision, AssistantToolCall)
        and _tool_repeat_limit_reached(graph_state, decision.tool_name)
    ):
        guard = LoopGuardDecision(
            True,
            "tool_repeat_limit_reached",
            (
                f"{decision.tool_name} may execute successfully at most once "
                "in one run; the additional call was blocked."
            ),
            disposition="finalize",
        )
        _record_loop_guard(graph_state, guard)
        return decision.model_copy(
            update={
                "reason": guard.message,
                "safety_notes": [*decision.safety_notes, guard.code],
            }
        )
    if (
        isinstance(decision, AssistantToolCall)
        and not _is_plan_mode_active(state)
        and LoopGuard(state.request.metadata).nonrecoverable_failure_already_seen(
            decision.tool_name
        )
    ):
        guard = LoopGuardDecision(
            True,
            "nonrecoverable_tool_retry_blocked",
            (
                f"{decision.tool_name} already reported a non-recoverable failure "
                "in this run; another call to the same tool was blocked."
            ),
            disposition="block_action",
        )
        _record_loop_guard(graph_state, guard)
        return decision.model_copy(
            update={
                "reason": guard.message,
                "safety_notes": [*decision.safety_notes, guard.code],
            }
        )
    if (
        isinstance(decision, AssistantToolCall)
        and not _is_plan_mode_active(state)
        and LoopGuard(state.request.metadata).complete_call_already_seen(
            tool_name=decision.tool_name,
            tool_input=decision.tool_input or {},
        )
    ):
        guard = LoopGuardDecision(
            True,
            "duplicate_complete_tool_call",
            (
                f"An identical complete {decision.tool_name} call already succeeded; "
                "answering from the existing result."
            ),
            disposition="finalize",
        )
        _record_loop_guard(graph_state, guard)
        return decision.model_copy(
            update={
                "reason": guard.message,
                "safety_notes": [*decision.safety_notes, guard.code],
            }
        )
    if (
        isinstance(decision, AssistantToolCall)
        and not _is_plan_mode_active(state)
        and LoopGuard(state.request.metadata).failed_call_already_seen(
            tool_name=decision.tool_name,
            tool_input=decision.tool_input or {},
        )
    ):
        guard = LoopGuardDecision(
            True,
            "duplicate_failed_tool_call",
            f"An identical failed {decision.tool_name} call was blocked before execution.",
            disposition="finalize",
        )
        _record_loop_guard(graph_state, guard)
        return decision.model_copy(
            update={
                "reason": guard.message,
                "safety_notes": [*decision.safety_notes, guard.code],
            }
        )
    return decision


def _tool_repeat_limit_reached(
    graph_state: AssistantLoopState,
    tool_name: str,
) -> bool:
    registry = getattr(graph_state.get("tool_executor"), "registry", None)
    get_spec = getattr(registry, "get_spec", None)
    if not callable(get_spec):
        return False
    try:
        spec = get_spec(tool_name)
    except KeyError:
        return False
    if spec.repeat_policy != "once_per_run":
        return False
    return any(
        record.tool_name == tool_name and record.status == "succeeded"
        for record in graph_state["state"].tool_calls
    )


def _apply_terminal_decision(
    graph_state: AssistantLoopState,
    decision: AssistantTurnOutput,
    context: AssistantDecisionContext,
) -> None:
    """Persist response state when the assistant returns text."""

    if not isinstance(decision, AssistantTextOutput):
        return

    state = graph_state["state"]
    if _is_plan_mode_active(state):
        _mark_plan_mode_status(state, "completed")
    if not state.tool_results:
        if _is_direct_chat_state(state):
            _set_direct_chat_response(graph_state, decision, context.iterations, context.tool_observations)
            return
        state.set_response(
            AgentResponse(
                message=decision.text,
                data={
                    "intent": state.intent.intent if state.intent else None,
                    "assistant_output": decision.type,
                    "reason": decision.reason,
                    "iterations": context.iterations,
                    "tool_observations": len(context.tool_observations),
                    "plan_status": state.plan_status,
                    "current_step_id": state.current_step_id,
                    "plan_revision_count": state.plan_revision_count,
                    **_fallback_response_data(decision),
                },
            )
        )
        state.status = "completed"
        return

    if _should_preserve_assistant_final_answer(decision=decision, is_mock=context.is_mock):
        _set_assistant_final_answer_response(graph_state, decision, context.iterations, context.tool_observations)
        state.status = "completed"
    elif state.status != "failed":
        state.status = "completed"


def _fallback_response_data(decision: AssistantTextOutput) -> dict[str, Any]:
    code = next(
        (note for note in decision.safety_notes if note in MAIN_LLM_NO_ANSWER_MESSAGES),
        None,
    )
    return {"fallback_reason": code} if code is not None else {}


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


def _with_finalize_protocol_retry_guidance(request: ChatRequest) -> ChatRequest:
    guidance = (
        "# Finalization protocol violation\n\n"
        "上一次输出违反了最终回答协议。本次只能输出面向用户的自然语言答案；"
        "任何工具调用、工具参数或继续执行计划都将被 Runtime 拒绝。"
    )
    messages = [dict(message) for message in request.messages]
    if messages and messages[0].get("role") == "system":
        content = str(messages[0].get("content") or "")
        messages[0]["content"] = f"{content}\n\n{guidance}"
    else:
        messages.insert(0, {"role": "system", "content": guidance})
    return request.model_copy(
        update={
            "messages": messages,
            "tools": [],
            "tool_choice": "none",
        }
    )


def _finalize_fallback(context: AssistantDecisionContext) -> AssistantTextOutput:
    return AssistantTextOutput(
        text=finalize_fallback_text(context.tool_observations),
        reason="Answer-only synthesis did not produce a usable final response.",
        safety_notes=["finalization_synthesis_failed"],
    )


def _mock_assistant_decision_from_plan(
    *,
    request: UserRequest,
    tool_observations: list[dict[str, Any]],
    state: AgentState,
    outputs_by_step: dict[str, ToolResult],
) -> AssistantTurnOutput:
    """Return the next deterministic ReAct decision from the rule-based plan."""

    if state.plan is None:
        return AssistantTextOutput(
            text="离线计划不可用，无法选择下一步工具。",
            reason="_decide_with_mock_plan(...) requires state.plan from the rule router.",
            safety_notes=["missing_rule_plan"],
        )

    if state.plan.requires_followup:
        return AssistantTextOutput(
            text=state.plan.followup_question or "请补充你想让我处理的对象或目标。",
            reason="计划缺少必要输入，需要追问用户。",
        )

    executable_steps = [step for step in state.plan.steps if step.tool_name is not None]
    next_index = len(state.tool_results)
    if next_index < len(executable_steps):
        step = executable_steps[next_index]
        from assistant_agent.runtime.tool_input_builder import build_tool_input

        return AssistantToolCall(
            tool_name=step.tool_name,
            tool_input=build_tool_input(step.action, request, outputs_by_step),
            reason=step.reason or f"执行计划步骤：{step.action}",
        )

    return AssistantTextOutput(
        text="计划步骤已执行完毕。",
        reason="计划步骤已执行完毕，交给响应合成器生成最终答复。",
    )


def _set_direct_chat_response(
    graph_state: AssistantLoopState,
    decision: AssistantTextOutput,
    iterations: int,
    tool_observations: list[dict[str, Any]],
) -> None:
    """Run direct_chat through the chat adapter so text-only ReAct has a contract."""

    state = graph_state["state"]
    request = graph_state["request"]
    memory_summaries = [
        memory.text
        for memory in (
            state.session_memory_snapshot.memories
            if state.session_memory_snapshot is not None
            else []
        )
    ]
    chat_request = _with_response_stream_callback(
        build_direct_chat_request(
            request,
            memory_context=memory_summaries,
        ),
        graph_state,
        source="direct_chat",
    )
    result = graph_state["chat_adapter"].chat(chat_request)
    errors = [error.model_dump(mode="json") for error in result.errors]
    contract = build_text_capability_output(
        capability="direct_chat",
        status="succeeded" if result.success else "failed",
        output_ref=result.output_ref,
        data={"response_text": result.response_text, "provider": result.provider, "model": result.model},
        errors=errors,
    )
    message = result.response_text if result.success else decision.text
    state.set_response(
        AgentResponse(
            message=message,
            data={
                "intent": state.intent.intent if state.intent else None,
                "assistant_output": decision.type,
                "reason": decision.reason,
                "iterations": iterations,
                "tool_observations": len(tool_observations),
                "tool_count": len(state.tool_calls),
                "provider": result.provider,
                "model": result.model,
                "usage": result.usage,
                "output_ref": result.output_ref,
                "errors": errors,
                "contract": contract,
                "plan_status": state.plan_status,
                "current_step_id": state.current_step_id,
                "plan_revision_count": state.plan_revision_count,
            },
            output_refs=[result.output_ref] if result.output_ref else [],
        )
    )


def _should_preserve_assistant_final_answer(*, decision: AssistantTextOutput, is_mock: bool) -> bool:
    """Return true when an assistant final answer should bypass response composition."""

    return (
        not is_mock
        and not _assistant_final_answer_is_technical_failure(decision)
    )


def _assistant_final_answer_is_technical_failure(decision: AssistantTextOutput) -> bool:
    """Detect provider self-repair/parsing messages that should not face users."""

    message = decision.text
    technical_messages = (
        "原始输出格式不完整",
        "无法正常解析",
        "助手决策输出格式不完整",
    )
    return any(marker in message for marker in technical_messages)


def _set_assistant_final_answer_response(
    graph_state: AssistantLoopState,
    decision: AssistantTextOutput,
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
    handled_tool_failures = sum(
        1 for error in state.errors if "tool_call_id" in error.details
    )
    state.set_response(
        AgentResponse(
            message=decision.text,
            data={
                "intent": state.intent.intent if state.intent else None,
                "final_answer_source": "assistant_loop",
                "assistant_output": decision.type,
                "reason": decision.reason,
                "iterations": iterations,
                "tool_count": len(state.tool_calls),
                "tool_observations": len(tool_observations),
                "contracts": contracts,
                "output_refs": output_refs,
                "errors": failures,
                "degraded": bool(failures),
                "handled_tool_failures": handled_tool_failures,
                "plan_status": state.plan_status,
                "current_step_id": state.current_step_id,
                "plan_revision_count": state.plan_revision_count,
            },
            output_refs=output_refs,
        )
    )


def _is_direct_chat_state(state: AgentState) -> bool:
    return state.intent is not None and canonical_intent(state.intent.intent) == "direct_chat"


def _with_response_stream_callback(
    request: ChatRequest,
    graph_state: AssistantLoopState,
    *,
    source: str,
) -> ChatRequest:
    callback = _response_stream_callback(graph_state, source=source)
    if callback is None:
        return request
    graph_state["response_stream_current_call_emitted"] = False
    return request.model_copy(update={"stream_callback": callback})


def _response_stream_callback(
    graph_state: AssistantLoopState,
    *,
    source: str,
) -> Any | None:
    event_sink = graph_state.get("event_sink")
    if event_sink is None:
        return None
    state = graph_state["state"]

    def emit_delta(text: str, payload: dict[str, Any]) -> None:
        if not text:
            return
        emitted_text = text
        emitted_payload = payload
        if graph_state.get("response_stream_separator_pending", False):
            graph_state["response_stream_separator_pending"] = False
            previous_has_newline = graph_state.get(
                "response_stream_ends_with_newline",
                False,
            )
            next_has_newline = text.startswith(("\n", "\r"))
            if not previous_has_newline and not next_has_newline:
                emitted_text = f"\n{text}"
                emitted_payload = {
                    **payload,
                    "runtime_separator_inserted": True,
                }
        event = stream_delta_to_agent_event(
            emitted_text,
            emitted_payload,
            session_id=state.session_id,
            run_id=state.run_id,
            source=source,
        )
        if event is None:
            return
        event_sink.emit(event)
        graph_state["response_stream_current_call_emitted"] = True
        graph_state["response_stream_ends_with_newline"] = emitted_text.endswith(
            ("\n", "\r")
        )

    return emit_delta


def _mark_response_stream_tool_boundary(
    graph_state: AssistantLoopState,
) -> None:
    if graph_state.get("response_stream_current_call_emitted", False):
        graph_state["response_stream_separator_pending"] = True


def _is_mock_chat_adapter(chat_adapter: ChatAdapter) -> bool:
    return getattr(chat_adapter, "provider", "") == "mock" or hasattr(chat_adapter, "MockChatAdapter")


def execute_requested_tool_node(graph_state: AssistantLoopState) -> AssistantLoopState:
    """Execute the current provider-native tool batch within the run budget."""

    pending = list(graph_state.get("pending_tool_calls") or [])
    if not pending:
        decision = graph_state.get("assistant_output")
        pending = [decision] if isinstance(decision, AssistantToolCall) else []
    if not pending:
        return graph_state

    current = graph_state
    max_tool_calls = int(graph_state.get("max_tool_iterations", _get_max_tool_iterations()))
    tool_calls_used = int(graph_state.get("tool_calls_used", 0))
    for index, decision in enumerate(pending):
        if tool_calls_used >= max_tool_calls:
            skipped = len(pending) - index
            metadata = current["state"].request.metadata
            _enter_finalize_phase(
                current,
                reason="tool_call_budget_exhausted",
                source="tool_budget",
            )
            metadata["tool_call_budget_exhausted"] = True
            metadata["tool_calls_skipped_for_budget"] = (
                int(metadata.get("tool_calls_skipped_for_budget", 0)) + skipped
            )
            break
        current = _execute_single_requested_tool_node(
            {
                **current,
                "assistant_output": decision,
            }
        )
        tool_calls_used += 1
        current["tool_calls_used"] = tool_calls_used
        if tool_calls_used >= max_tool_calls:
            _enter_finalize_phase(
                current,
                reason="tool_call_budget_exhausted",
                source="tool_budget",
            )
        if current["state"].status in {"failed", "cancelled"}:
            break

    current["pending_tool_calls"] = []
    return current


def _tool_result_is_nonrecoverable(result: ToolResult) -> bool:
    """Read the structured recovery contract without guessing from error text."""

    if result.success:
        return False
    if result.contract is not None and any(
        error.recoverable is False for error in result.contract.errors
    ):
        return True
    for payload in (result.model_observation, result.data):
        if not isinstance(payload, dict):
            continue
        errors = payload.get("errors")
        if not isinstance(errors, list):
            continue
        if any(
            isinstance(error, dict) and error.get("recoverable") is False
            for error in errors
        ):
            return True
    return False


def _execute_single_requested_tool_node(graph_state: AssistantLoopState) -> AssistantLoopState:
    """
    Execute the tool requested by the assistant.

    Reads the assistant tool output, validates it, runs the tool,
    and stores the observation for the next iteration.
    """
    state = graph_state["state"]
    decision = graph_state.get("assistant_output")
    tool_executor = graph_state["tool_executor"]
    tool_observations = graph_state.get("tool_observations", [])

    if not isinstance(decision, AssistantToolCall):
        return graph_state

    tool_name = decision.tool_name
    tool_input = decision.tool_input
    repeat_limit_reached = (
        "tool_repeat_limit_reached" in decision.safety_notes
        or _tool_repeat_limit_reached(graph_state, tool_name)
    )
    if repeat_limit_reached:
        if "tool_repeat_limit_reached" not in decision.safety_notes:
            _record_loop_guard(
                graph_state,
                LoopGuardDecision(
                    True,
                    "tool_repeat_limit_reached",
                    (
                        f"{tool_name} may execute successfully at most once "
                        "in one run; the additional call was blocked."
                    ),
                    disposition="finalize",
                ),
            )
        _enter_finalize_phase(
            graph_state,
            reason="tool_repeat_limit_reached",
            source="loop_guard",
        )
        observation = rejected_observation(
            tool_name=tool_name or "unknown",
            code="tool_repeat_limit_reached",
            message="This tool already completed successfully in this run.",
        )
        return {
            **graph_state,
            "tool_observations": _record_react_observation(
                graph_state,
                tool_observations,
                observation,
            ),
        }
    if "nonrecoverable_tool_retry_blocked" in decision.safety_notes:
        observation = rejected_observation(
            tool_name=tool_name or "unknown",
            code="nonrecoverable_tool_retry_blocked",
            message=(
                "This tool already reported a non-recoverable failure in this run."
            ),
        )
        return {
            **graph_state,
            "tool_observations": _record_react_observation(
                graph_state,
                tool_observations,
                observation,
            ),
        }
    if "duplicate_failed_tool_call" in decision.safety_notes:
        observation = rejected_observation(
            tool_name=tool_name or "unknown",
            code="duplicate_failed_tool_call",
            message="An identical failed tool call was blocked before execution.",
        )
        _enter_finalize_phase(
            graph_state,
            reason="duplicate_failed_tool_call",
            source="loop_guard",
        )
        return {
            **graph_state,
            "tool_observations": _record_react_observation(
                graph_state,
                tool_observations,
                observation,
            ),
        }
    if (
        "duplicate_complete_tool_call" in decision.safety_notes
        or LoopGuard(state.request.metadata).complete_call_already_seen(
            tool_name=tool_name,
            tool_input=tool_input or {},
        )
    ):
        observation = rejected_observation(
            tool_name=tool_name or "unknown",
            code="duplicate_complete_tool_call",
            message="An identical complete tool call was blocked before execution.",
        )
        _enter_finalize_phase(
            graph_state,
            reason="duplicate_complete_tool_call",
            source="loop_guard",
        )
        return {
            **graph_state,
            "tool_observations": _record_react_observation(
                graph_state,
                tool_observations,
                observation,
            ),
        }
    step, plan_rejection = _current_plan_step(state, decision, graph_state["outputs_by_step"])
    if plan_rejection is not None:
        rejection_error = plan_rejection.error
        state.errors.append(
            AgentError(
                message=(
                    rejection_error.message
                    if rejection_error is not None
                    else plan_rejection.summary
                ),
                source=tool_name or "plan_mode",
                details={
                    "code": (
                        rejection_error.code
                        if rejection_error is not None
                        else "plan_step_rejected"
                    ),
                    "recovery_action": "revise_plan",
                },
            )
        )
        return {
            **graph_state,
            "tool_observations": _record_react_observation(graph_state, tool_observations, plan_rejection),
        }
    step_id = step.step_id if step is not None else f"assistant_loop_{len(tool_observations) + 1}"
    if step is not None:
        state.current_step_id = step.step_id

    validation = ActionValidator().validate(
        decision=decision,
        registry=tool_executor.registry,
        request=graph_state["request"],
        state=state,
    )
    state.request.metadata["last_action_validator"] = validation.model_dump(mode="json")
    _append_trace(
        graph_state,
        event_type="observability",
        canonical_event="action.validation.finished",
        observation_type="span",
        observation_scope="iteration",
        status="accepted" if validation.accepted else "rejected",
        tool_name=tool_name,
        output_summary={"validator_result": validation.model_dump(mode="json")},
        attributes=validation.model_dump(mode="json"),
        error={"code": validation.code, "message": validation.message} if not validation.accepted else None,
    )
    if not validation.accepted:
        error = AgentError(
            message=validation.message,
            source=tool_name or "assistant_loop",
            details={"code": validation.code, "recovery_action": "stop_with_error", "validator_result": validation.model_dump(mode="json")},
        )
        state.errors.append(error)
        observation = rejected_observation(
            tool_name=tool_name or "unknown",
            code=validation.code,
            message=validation.message,
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
                        "assistant_output": "text",
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
            validated_input=validation.validated_input,
            failure_mode="continue_to_model",
            progress_message=decision.progress_message,
        )

        observation = observation_from_tool_result(
            result,
        )
        tool_call_id, source_tool_span_id = _latest_tool_execution_correlation(
            graph_state,
            tool_name=tool_name,
        )
        if _is_plan_mode_active(state) and not result.success:
            _mark_plan_mode_status(state, "replanning")
        tool_spec = tool_executor.registry.get_spec(tool_name)
        if not result.success and tool_spec.category in {"write", "dangerous"}:
            _enter_finalize_phase(
                graph_state,
                reason="failed_side_effect_tool",
                source="tool_failure",
            )
        if result.success:
            LoopGuard(state.request.metadata).record_complete_tool_success(
                tool_name=tool_name,
                tool_input=tool_input,
            )
        recorded_guard = LoopGuard(state.request.metadata).record_tool_result(
            tool_name=tool_name,
            tool_input=tool_input,
            success=result.success,
            nonrecoverable=_tool_result_is_nonrecoverable(result),
        )
        guard = (
            LoopGuardDecision(False, "ok", "Guard not triggered for optional step.")
            if step is not None and step.optional
            else recorded_guard
        )
        if guard.triggered:
            _record_loop_guard(graph_state, guard)
            if (
                not _is_plan_mode_active(state)
                and guard.disposition == "finalize"
            ):
                _enter_finalize_phase(
                    graph_state,
                    reason=guard.code,
                    source="loop_guard",
                )

        outputs_by_step = {
            **graph_state["outputs_by_step"],
            step_id: result,
        }
        if step is not None:
            _advance_plan_after_tool_result(state, outputs_by_step, result)
        return {
            **graph_state,
            "tool_observations": _record_react_observation(
                graph_state,
                tool_observations,
                observation,
                tool_call_id=tool_call_id,
                source_tool_span_id=source_tool_span_id,
                content_export_policy=str(
                    getattr(
                        tool_executor.registry.get(tool_name),
                        "trace_content_policy",
                        "default",
                    )
                ),
                tool_result=result,
            ),
            "outputs_by_step": outputs_by_step,
        }
    except AgentRunCancelled:
        raise
    except Exception as e:
        error = AgentError(
            message=f"工具执行异常：{str(e)}",
            source=tool_name,
            details={"tool_input": tool_input},
        )
        state.errors.append(error)
        observation = rejected_observation(
            tool_name=tool_name or "unknown",
            code="tool_exception",
            message=str(e),
        )
        return {
            **graph_state,
            "tool_observations": _record_react_observation(graph_state, tool_observations, observation),
        }


def _current_plan_step(
    state: AgentState,
    decision: AssistantToolCall,
    outputs_by_step: dict[str, ToolResult],
) -> tuple[TaskStep | None, ToolObservation | None]:
    if not _is_plan_mode_active(state):
        return _legacy_current_plan_step(state, decision.tool_name), None
    if state.plan is None:
        return None, None

    if decision.step_id:
        step = _plan_step_by_id(state.plan, decision.step_id)
        if step is None:
            return None, rejected_observation(
                tool_name=decision.tool_name or "unknown",
                code="unknown_step",
                message=f"Unknown plan step: {decision.step_id}.",
            )
    else:
        step = _next_matching_plan_step(state.plan, decision.tool_name, outputs_by_step)
        if step is None:
            return None, rejected_observation(
                tool_name=decision.tool_name or "unknown",
                code="plan_step_not_found",
                message=f"Tool {decision.tool_name or 'unknown'} is not part of the active plan.",
            )

    if step.tool_name is None:
        return None, rejected_observation(
            tool_name=decision.tool_name or "unknown",
            code="non_executable_step",
            message=f"Plan step {step.step_id} has no executable tool.",
        )
    if step.tool_name != decision.tool_name:
        return None, rejected_observation(
            tool_name=decision.tool_name or "unknown",
            code="plan_tool_mismatch",
            message=f"Plan step {step.step_id} requires {step.tool_name}, not {decision.tool_name}.",
        )
    dependency_error = _dependency_error(step, outputs_by_step)
    if dependency_error is not None:
        return None, rejected_observation(
            tool_name=step.tool_name,
            code="dependency_not_satisfied",
            message=dependency_error,
        )
    return step, None


def _legacy_current_plan_step(state: AgentState, tool_name: str | None) -> TaskStep | None:
    if state.plan is None or tool_name is None:
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


def _is_plan_mode_active(state: AgentState) -> bool:
    marker = state.request.metadata.get("plan_mode")
    return (
        isinstance(marker, dict)
        and marker.get("active") is True
        and state.plan is not None
        and state.plan_status in {"active", "replanning"}
    )


def _mark_plan_mode_status(state: AgentState, status: str) -> None:
    if status in {"none", "active", "replanning", "completed", "failed"}:
        state.plan_status = cast(Any, status)
    active = state.plan is not None and state.plan_status in {"active", "replanning"}
    state.request.metadata["plan_status"] = state.plan_status
    state.request.metadata["plan_mode"] = {"active": active}
    if state.plan is not None:
        state.request.metadata["current_plan"] = state.plan.model_dump(mode="json")
    state.request.metadata["current_step_id"] = state.current_step_id
    state.request.metadata["plan_revision_count"] = state.plan_revision_count


def _plan_step_by_id(plan: TaskPlan | None, step_id: str | None) -> TaskStep | None:
    if plan is None or step_id is None:
        return None
    for step in plan.steps:
        if step.step_id == step_id:
            return step
    return None


def _next_matching_plan_step(
    plan: TaskPlan,
    tool_name: str | None,
    outputs_by_step: dict[str, ToolResult],
) -> TaskStep | None:
    for step in plan.steps:
        if step.tool_name != tool_name:
            continue
        result = outputs_by_step.get(step.step_id)
        if result is None or not result.success:
            return step
    return None


def _next_pending_plan_step_id(plan: TaskPlan, outputs_by_step: dict[str, ToolResult]) -> str | None:
    for step in plan.steps:
        if step.tool_name is None:
            continue
        result = outputs_by_step.get(step.step_id)
        if result is None or not result.success:
            return step.step_id
    return None


def _dependency_error(step: TaskStep, outputs_by_step: dict[str, ToolResult]) -> str | None:
    for dependency in step.depends_on:
        result = outputs_by_step.get(dependency)
        if result is None or not result.success:
            return f"Step {step.step_id} depends on unfinished step {dependency}."
    return None


def _advance_plan_after_tool_result(
    state: AgentState,
    outputs_by_step: dict[str, ToolResult],
    result: ToolResult,
) -> None:
    if state.plan is None:
        return
    if not result.success:
        _mark_plan_mode_status(state, "replanning")
        return
    next_step_id = _next_pending_plan_step_id(state.plan, outputs_by_step)
    state.current_step_id = next_step_id
    _mark_plan_mode_status(state, "completed" if next_step_id is None else "active")


def _route_with_optional_request(router: ToolRouter, intent, request: UserRequest):
    if len(signature(router.route).parameters) >= 2:
        return router.route(intent, request)
    return router.route(intent)


def _select_tools_with_optional_request(router: ToolRouter, intent, request: UserRequest):
    if len(signature(router.select_tools).parameters) >= 2:
        return router.select_tools(intent, request)
    return router.select_tools(intent)


def route_after_assistant(graph_state: AssistantLoopState) -> str:
    """Route strict assistant text/tool output."""
    state = graph_state["state"]
    output = graph_state.get("assistant_output")

    if state.status == "failed":
        return "finish"

    if state.status == "completed":
        return "finish"

    if output is None:
        return "finish"

    if isinstance(output, AssistantToolCall):
        return "execute_tool"

    return "finish"


def _get_tool_context(state: AgentState) -> Any:
    """Get tool context from agent state."""
    try:
        from assistant_agent.tools.base import ToolContext
        return ToolContext(
            run_id=state.run_id,
            user_id=state.user_id,
            session_id=state.session_id,
        )
    except Exception:
        return None


def _record_react_decision(
    graph_state: AssistantLoopState,
    decision: AssistantTurnOutput,
    iteration: int,
    *,
    context: AssistantDecisionContext | None = None,
) -> None:
    """Record one strict assistant text/tool output from the ReAct iteration."""

    state = graph_state["state"]
    steps = state.request.metadata.setdefault("assistant_loop_steps", [])
    if not isinstance(steps, list):
        steps = []
        state.request.metadata["assistant_loop_steps"] = steps
    trace_event = _decision_trace_event(decision, iteration)
    decision_trace = state.request.metadata.setdefault("decision_trace", [])
    if isinstance(decision_trace, list):
        decision_trace.append(trace_event)
    is_tool_call = isinstance(decision, AssistantToolCall)
    steps.append({
        "iteration": iteration + 1,
        "output_type": decision.type,
        "tool_name": decision.tool_name if is_tool_call else None,
        "tool_input": decision.tool_input if is_tool_call else {},
        "step_id": decision.step_id if is_tool_call else None,
        "message": decision.text if isinstance(decision, AssistantTextOutput) else None,
        "reason": decision.reason,
        "safety_notes": decision.safety_notes,
        "plan_status": state.plan_status,
        "run_phase": _run_phase(graph_state).value,
    })
    output_summary = {
        "output_type": decision.type,
        "reason": decision.reason,
        "confidence": decision.confidence if is_tool_call else None,
        "message_present": isinstance(decision, AssistantTextOutput),
        "step_id": decision.step_id if is_tool_call else None,
        "plan_status": state.plan_status,
        "run_phase": _run_phase(graph_state).value,
    }
    _runtime_event_publisher(graph_state).publish_assistant_step(
        AssistantStepFact(
            state=state,
            trace_id=graph_state.get("trace_id"),
            node_name=graph_state.get("current_node_name", "assistant_loop"),
            decision_trace=trace_event,
            trace_event_type="assistant_output",
            canonical_event="assistant.output",
            observation_type=None,
            observation_scope="runtime",
            status=decision.type,
            tool_name=decision.tool_name if is_tool_call else None,
            output_summary=output_summary,
            attributes={
                "iteration": iteration + 1,
                "output_type": decision.type,
                "tool_name": decision.tool_name if is_tool_call else None,
                "step_id": decision.step_id if is_tool_call else None,
                "plan_status": state.plan_status,
                "safety_notes": decision.safety_notes,
                "run_phase": _run_phase(graph_state).value,
            },
        )
    )


def _selection_vector_hit_count(selection: dict[str, Any]) -> int:
    signal = selection.get("vector_shadow_signal")
    if not isinstance(signal, dict):
        return 0
    value = signal.get("hit_count")
    return value if isinstance(value, int) and value >= 0 else 0


def _metadata_int(metadata: dict[str, Any], key: str) -> int:
    value = metadata.get(key)
    return value if isinstance(value, int) and value >= 0 else 0


def _record_react_observation(
    graph_state: AssistantLoopState,
    existing: list[dict[str, Any]],
    observation: ToolObservation | dict[str, Any],
    *,
    tool_call_id: str | None = None,
    source_tool_span_id: str | None = None,
    content_export_policy: str = "default",
    tool_result: ToolResult | None = None,
) -> list[dict[str, Any]]:
    """Append a tool observation to both graph state and demo metadata."""

    state = graph_state["state"]
    payload = dict(
        observation.model_dump(mode="json")
        if isinstance(observation, ToolObservation)
        else observation
    )
    decision = graph_state.get("assistant_output")
    if (
        isinstance(decision, AssistantToolCall)
        and decision.provider_tool_call_id
    ):
        payload[PROVIDER_TOOL_CALL_ID_KEY] = decision.provider_tool_call_id
    observations = existing + [payload]
    trace_payload = _trace_safe_tool_observation(
        payload,
        content_export_policy=content_export_policy,
    )
    observation_error = trace_payload.get("error")
    if not isinstance(observation_error, dict):
        observation_error = None
    from assistant_agent.observability.trace_conversation import (
        TraceToolResult,
        TraceToolObservation,
        get_default_trace_conversation_store,
    )
    from assistant_agent.observability.visual_trace_content import (
        sanitize_visual_trace_content,
    )

    trace_content_store = get_default_trace_conversation_store()
    if (
        tool_result is not None
        and source_tool_span_id is not None
        and content_export_policy == "metadata_only"
    ):
        trace_content_store.append_tool_result(
            user_id=state.user_id,
            session_id=state.session_id,
            trace_id=graph_state.get("trace_id") or state.trace_id,
            tool_result=TraceToolResult(
                span_id=source_tool_span_id,
                tool_name=tool_result.tool_name,
                result=_safe_visual_tool_result_content(
                    tool_result,
                    observation=payload,
                ),
            ),
        )
    trace_content_store.append_tool_observation(
        user_id=state.user_id,
        session_id=state.session_id,
        trace_id=graph_state.get("trace_id") or state.trace_id,
        tool_observation=TraceToolObservation(
            observation_index=len(observations),
            tool_name=str(payload.get("tool_name") or "unknown"),
            observation=(
                sanitize_visual_trace_content(payload)
                if content_export_policy == "metadata_only"
                else dict(payload)
            ),
            source_tool_span_id=source_tool_span_id,
            runtime_tool_call_id=tool_call_id,
            provider_tool_call_id=(
                decision.provider_tool_call_id
                if isinstance(decision, AssistantToolCall)
                else None
            ),
        ),
    )
    trace_event = _observation_trace_event(
        trace_payload,
        len(observations),
        tool_call_id=tool_call_id,
        source_tool_span_id=source_tool_span_id,
    )
    decision_trace = state.request.metadata.setdefault("decision_trace", [])
    if isinstance(decision_trace, list):
        decision_trace.append(trace_event)
    steps = state.request.metadata.setdefault("assistant_loop_steps", [])
    if isinstance(steps, list):
        steps.append(
            {
                "iteration": len(observations),
                "observation_tool": trace_payload.get("tool_name"),
                "status": trace_payload.get("status"),
                "success": trace_payload.get("status") == "succeeded",
                "summary": trace_payload.get("summary"),
                "output_ref": trace_payload.get("output_ref"),
                "error": observation_error,
            }
        )
    _runtime_event_publisher(graph_state).publish_assistant_step(
        AssistantStepFact(
            state=state,
            trace_id=graph_state.get("trace_id"),
            node_name=graph_state.get("current_node_name", "assistant_loop"),
            decision_trace=trace_event,
            trace_event_type="tool_observation",
            canonical_event="tool.observation",
            observation_type="event",
            observation_scope="iteration",
            status=trace_payload.get("status"),
            tool_name=trace_payload.get("tool_name"),
            output_summary={
                "summary": trace_payload.get("summary"),
                "output_ref": trace_payload.get("output_ref"),
            },
            attributes={
                key: value
                for key, value in {
                    "observation_index": len(observations),
                    "summary": trace_payload.get("summary"),
                    "output_ref": trace_payload.get("output_ref"),
                    "tool_call_id": tool_call_id,
                    "source_tool_span_id": source_tool_span_id,
                    "content_export_policy": content_export_policy,
                }.items()
                if value is not None
            },
            trace_error=observation_error,
        )
    )
    return observations


def _safe_visual_tool_result_content(
    result: ToolResult,
    *,
    observation: dict[str, Any],
) -> dict[str, Any]:
    from assistant_agent.observability.visual_trace_content import (
        sanitize_visual_tool_result,
    )

    safe_data = sanitize_tool_observation_detail(
        result.data if isinstance(result.data, dict) else {}
    )
    error = observation.get("error")
    return sanitize_visual_tool_result(
        {
            "tool_name": result.tool_name,
            "success": result.success,
            "output_ref": result.output_ref,
            "data": safe_data if isinstance(safe_data, dict) else {},
            "error": dict(error) if isinstance(error, dict) else None,
        }
    )


def _trace_safe_tool_observation(
    payload: dict[str, Any],
    *,
    content_export_policy: str,
) -> dict[str, Any]:
    if content_export_policy != "metadata_only":
        return payload
    error = payload.get("error")
    safe_error = None
    if isinstance(error, dict):
        safe_error = {
            "code": error.get("code") or "tool_failed",
            "message": "Tool observation failed.",
            "retryable": bool(error.get("retryable", False)),
        }
    return {
        "tool_name": payload.get("tool_name") or "unknown",
        "status": payload.get("status") or "failed",
        "outcome": payload.get("outcome"),
        "is_complete": bool(payload.get("is_complete", False)),
        "error": safe_error,
    }


def _decision_trace_event(decision: AssistantTurnOutput, iteration: int) -> dict[str, Any]:
    is_tool_call = isinstance(decision, AssistantToolCall)
    event_name = "decision" if is_tool_call else "final_answer"
    payload: dict[str, Any] = {
        "iteration": iteration + 1,
        "event": event_name,
        "output_type": decision.type,
        "decision_summary": decision.reason or "",
    }
    if is_tool_call:
        payload["action"] = decision.tool_name
        payload["action_input"] = decision.tool_input
        if decision.step_id:
            payload["step_id"] = decision.step_id
    else:
        payload["answer"] = decision.text
    return payload


def _observation_trace_event(
    payload: dict[str, Any],
    iteration: int,
    *,
    tool_call_id: str | None = None,
    source_tool_span_id: str | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "iteration": iteration,
        "event": "observation",
        "action": payload.get("tool_name") or "unknown",
        "success": payload.get("status") == "succeeded",
        "output_ref": payload.get("output_ref"),
        "output_preview": payload.get("summary"),
        "tool_call_id": tool_call_id,
        "source_tool_span_id": source_tool_span_id,
    }
    error = payload.get("error")
    if isinstance(error, dict):
        event["error"] = dict(error)
    return {key: value for key, value in event.items() if value is not None}


def _latest_tool_execution_correlation(
    graph_state: AssistantLoopState,
    *,
    tool_name: str,
) -> tuple[str | None, str | None]:
    """Resolve the canonical terminal event for the tool result just returned."""

    state = graph_state["state"]
    call = next(
        (
            item
            for item in reversed(state.tool_calls)
            if item.tool_name == tool_name and item.finished_at is not None
        ),
        None,
    )
    if call is None:
        return None, None
    trace_store = graph_state.get("trace_store")
    if trace_store is None:
        return call.tool_call_id, None
    terminal = next(
        (
            event
            for event in reversed(trace_store.list_by_run(state.run_id))
            if event.canonical_event in {"tool.finished", "tool.failed"}
            and event.attributes.get("tool_call_id") == call.tool_call_id
        ),
        None,
    )
    return call.tool_call_id, terminal.span_id if terminal is not None else None


def _runtime_event_publisher(
    graph_state: AssistantLoopState,
) -> RuntimeEventPublisher:
    tool_executor = graph_state.get("tool_executor")
    return RuntimeEventPublisher(
        event_sink=getattr(tool_executor, "event_sink", None),
        trace_store=graph_state.get("trace_store"),
    )


def _guard_final_answer(guard: LoopGuardDecision) -> AssistantTextOutput:
    return AssistantTextOutput(
        text="工具调用保护已触发，我已停止继续调用工具。请补充更明确的信息，或稍后重试。",
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
        error=(
            observation.error.model_dump(mode="json")
            if observation.error is not None
            else None
        ),
    )


def _record_loop_guard(graph_state: AssistantLoopState, guard: LoopGuardDecision) -> None:
    current_phase = _run_phase(graph_state)
    _append_trace(
        graph_state,
        event_type="loop_guard_triggered",
        canonical_event="loop_guard.triggered",
        status="triggered",
        attributes={
            "guard_code": guard.code,
            "disposition": guard.disposition,
            "from_phase": current_phase.value,
            "to_phase": (
                RunPhase.FINALIZE.value
                if guard.disposition == "finalize"
                else current_phase.value
            ),
        },
        error={"code": guard.code, "message": guard.message},
    )


def _record_tool_description_error(graph_state: AssistantLoopState, exc: Exception) -> None:
    """Record a lightweight diagnostic when tool descriptions are unavailable."""

    state = graph_state["state"]
    error = {
        "code": "tool_description_unavailable",
        "message": str(exc) or exc.__class__.__name__,
    }
    state.request.metadata["tool_description_error"] = error
    _append_trace(
        graph_state,
        event_type="assistant_decision",
        status="failed",
        output_summary={"diagnostic": "tool_description_unavailable"},
        error=error,
    )


def _append_trace(
    graph_state: AssistantLoopState,
    *,
    event_type: str,
    canonical_event: str | None = None,
    observation_type: TraceObservationType | None = None,
    observation_scope: TraceObservationScope | None = None,
    status: str | None = None,
    tool_name: str | None = None,
    output_summary: dict[str, Any] | None = None,
    attributes: dict[str, Any] | None = None,
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
            user_id=state.user_id,
            session_id=state.session_id,
            node_name=graph_state.get("current_node_name", "assistant_loop"),
            event_type=cast(Any, event_type),
            canonical_event=canonical_event,
            observation_type=observation_type or _point_observation_type(event_type),
            observation_scope=observation_scope or _point_observation_scope(event_type),
            tool_name=tool_name,
            status=status,
            output_summary=output_summary or {},
            attributes=attributes or {},
            error={
                "code": error.get("code"),
                "message": sanitize_trace_value(str(error.get("message", ""))),
            }
            if error
            else None,
        )
    )


def _point_observation_type(event_type: str) -> TraceObservationType | None:
    if event_type in {"assistant_decision", "tool_observation", "loop_guard_triggered"}:
        return "event"
    return None


def _point_observation_scope(event_type: str) -> TraceObservationScope:
    if _point_observation_type(event_type) is not None:
        return "iteration"
    return "runtime"


def _get_max_tool_iterations() -> int:
    """Get the maximum number of tool iterations from config or default."""
    import os
    try:
        from assistant_agent.config import ProviderConfig
        config = ProviderConfig.from_env()
        if hasattr(config, "max_tool_iterations"):
            return config.max_tool_iterations
    except Exception:
        pass
    try:
        return int(os.environ.get("MAX_TOOL_ITERATIONS", "5"))
    except ValueError:
        return 5
