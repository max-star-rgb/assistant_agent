"""Controlled assistant loop nodes.

In real chat-adapter mode, the LLM uses provider-native responses: natural
language content for direct answers, or native tool_calls for tool requests. In
mock mode, the rule plan provides deterministic decisions for stable offline
tests.

Local code owns the minimum required guardrails around those decisions:
tool listing, native tool-call normalization, validation, execution, loop
limits, trace recording, and state mutation.
"""

from dataclasses import dataclass
from inspect import signature
from time import perf_counter
from typing import Any, NotRequired, TypedDict, cast

from assistant_agent.agent.action_validator import ActionValidator
from assistant_agent.agent.cancellation import AgentRunCancelled
from assistant_agent.agent.intent import IntentDetector
from assistant_agent.agent.llm_event_mapping import stream_delta_to_agent_event
from assistant_agent.agent.loop_guard import LoopGuard, LoopGuardDecision
from assistant_agent.agent.memory_tool_selection import (
    build_memory_tool_selection_audit,
    record_memory_tool_selection_audit,
)
from assistant_agent.agent.plan_validator import PlanValidationResult, PlanValidator
from assistant_agent.agent.prompt_builder import build_direct_chat_request, build_text_capability_output
from assistant_agent.agent.router import ToolRouter
from assistant_agent.agent.state import AgentError, AgentState
from assistant_agent.agent.system_prompt_policy import (
    SystemPromptOptions,
    SystemPromptProfile,
    render_system_instruction,
)
from assistant_agent.agent.tool_executor import ToolExecutor
from assistant_agent.schemas.assistant_decision import AssistantDecision, native_tool_call_to_assistant_decision
from assistant_agent.schemas.capabilities import canonical_intent
from assistant_agent.schemas.context import AssistantContextPack
from assistant_agent.schemas.events import AgentEvent
from assistant_agent.schemas.planning import TaskPlan, TaskStep
from assistant_agent.schemas.requests import AgentResponse, UserRequest
from assistant_agent.schemas.tool_observation import (
    ToolObservation,
    observation_from_tool_result,
    rejected_observation,
)
from assistant_agent.schemas.tools import ToolResult, ToolSpec
from assistant_agent.services.chat_adapter import ChatAdapter, ChatRequest, ChatResult
from assistant_agent.services.context.observability import build_traced_assistant_context_pack
from assistant_agent.services.context.prompt_compiler import (
    PromptCompileMode,
    PromptCompileRequest,
    PromptCompileResult,
    PromptCompiler,
    owner_persona_for_pack,
    prompt_tool_specs_for_mode,
)
from assistant_agent.services.context.report import build_context_report
from assistant_agent.services.context.token_budget import normalize_provider_token_usage
from assistant_agent.services.trace_store import (
    TraceEvent,
    append_observability_event,
    new_span_id,
    sanitize_trace_value,
)


MAX_PLAN_STEPS = 8
MAX_PLAN_REVISIONS = 2
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
    context_compactor: NotRequired[Any]
    context_projector: NotRequired[Any]
    memory_manager: NotRequired[Any]
    outputs_by_step: dict[str, ToolResult]
    current_step_index: int
    trace_id: NotRequired[str]
    trace_store: NotRequired[Any]
    event_sink: NotRequired[Any]
    assistant_decision: NotRequired[AssistantDecision | None]
    pending_tool_decisions: NotRequired[list[AssistantDecision]]
    assistant_iterations: NotRequired[int]
    tool_calls_used: NotRequired[int]
    tool_observations: NotRequired[list[dict[str, Any]]]
    current_node_name: NotRequired[str]
    max_tool_iterations: NotRequired[int]
    max_plan_steps: NotRequired[int]
    max_plan_revisions: NotRequired[int]


@dataclass(frozen=True)
class AssistantDecisionContext:
    """Read-only inputs used by assistant decision policy."""

    context_pack: AssistantContextPack
    request: UserRequest
    memory_summaries: list[str]
    memory_text: str
    tool_specs: list[ToolSpec]
    tool_observations: list[dict[str, Any]]
    iterations: int
    max_iterations: int
    is_mock: bool


class _ResponseDeltaBuffer:
    """Hold streamed text until the model response is known to be terminal text."""

    def __init__(self, graph_state: AssistantLoopState, *, source: str) -> None:
        self._event_sink = graph_state.get("event_sink")
        self._state = graph_state["state"]
        self._source = source
        self._events: list[AgentEvent] = []

    @property
    def enabled(self) -> bool:
        return self._event_sink is not None

    def emit_delta(self, text: str, payload: dict[str, Any]) -> None:
        event = stream_delta_to_agent_event(
            text,
            payload,
            session_id=self._state.session_id,
            run_id=self._state.run_id,
            source=self._source,
        )
        if event is not None:
            self._events.append(event)

    def flush(self) -> None:
        if self._event_sink is not None:
            for event in self._events:
                self._event_sink.emit(event)
        self._events.clear()

    def discard(self) -> None:
        self._events.clear()


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
    decision = _apply_memory_tool_selection_policy(graph_state, decision, context)
    decision = _apply_decision_guards(graph_state, decision, context)
    _record_react_decision(graph_state, decision, iterations, context=context)
    _apply_terminal_decision(graph_state, decision, context)

    return _assistant_node_result(graph_state, decision, iterations)


def _assistant_node_result(
    graph_state: AssistantLoopState,
    decision: AssistantDecision,
    iterations: int,
) -> AssistantLoopState:
    return {
        **graph_state,
        "assistant_decision": decision,
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
    _project_request_context(graph_state, request)
    tool_calls_used = int(graph_state.get("tool_calls_used", len(tool_observations)))
    if tool_calls_used >= max_iterations:
        tool_specs = []
        request.metadata["tool_call_budget_exhausted"] = True
    else:
        try:
            tool_specs = _list_tool_specs(graph_state["tool_executor"].registry)
        except Exception as exc:
            tool_specs = []
            _record_tool_description_error(graph_state, exc)
    context_pack = build_traced_assistant_context_pack(
        trace_store=graph_state.get("trace_store"),
        trace_id=graph_state.get("trace_id"),
        node_name=graph_state.get("current_node_name", "assistant"),
        state=graph_state["state"],
        request=request,
        observations=tool_observations,
        tool_specs=tool_specs,
        iteration=iterations,
        max_iterations=max_iterations,
        context_compactor=graph_state.get("context_compactor"),
        registry_generation=getattr(
            graph_state["tool_executor"].registry,
            "generation",
            None,
        ),
        host_configured_tool_names=_host_configured_tool_names(
            graph_state["tool_executor"].registry
        ),
    )
    return AssistantDecisionContext(
        context_pack=context_pack,
        request=context_pack.request,
        memory_summaries=context_pack.memory_summaries,
        memory_text=context_pack.memory_text,
        tool_specs=context_pack.tool_specs,
        tool_observations=context_pack.observations,
        iterations=iterations,
        max_iterations=max_iterations,
        is_mock=is_mock,
    )


def _list_tool_specs(registry: Any) -> list[ToolSpec]:
    """Read ToolSpec contracts, falling back to legacy descriptions when needed."""

    if hasattr(registry, "list_specs"):
        specs = registry.list_specs()
        return [spec if isinstance(spec, ToolSpec) else ToolSpec.model_validate(spec) for spec in specs]
    descriptions = registry.describe_tools()
    return [ToolSpec.model_validate(item) for item in descriptions]


def _host_configured_tool_names(registry: Any) -> set[str]:
    provider = getattr(registry, "host_configured_tool_names", None)
    if not callable(provider):
        return set()
    return set(provider())


def _decide_next_action(
    graph_state: AssistantLoopState,
    *,
    context: AssistantDecisionContext,
    chat_adapter: ChatAdapter,
    state: AgentState,
) -> tuple[AssistantDecision, AssistantDecisionContext]:
    """Select the next assistant action without mutating response state."""

    if context.is_mock:
        return _decide_with_mock_plan(graph_state, context, state), context
    return _decide_with_llm(chat_adapter, context, state, graph_state=graph_state)


def _decide_with_mock_plan(
    graph_state: AssistantLoopState,
    context: AssistantDecisionContext,
    state: AgentState,
) -> AssistantDecision:
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
) -> tuple[AssistantDecision, AssistantDecisionContext]:
    """Ask the real chat adapter for the next ReAct action."""

    use_native_tools = _use_native_tool_calling(context, chat_adapter)
    if not use_native_tools:
        return (
            AssistantDecision(
                type="final_answer",
                message="当前模型不支持原生工具调用，无法执行 agent runtime。",
                reason="Provider-native tool calling is required; legacy JSON controller is disabled.",
                safety_notes=["native_tool_calling_unsupported"],
            ),
            context,
        )
    request, stream_buffer = _with_buffered_response_stream(
        _build_native_tool_chat_request(context, state),
        graph_state,
        source="assistant_langgraph_answer",
    )
    result = _run_chat_turn(graph_state, chat_adapter, request)
    _record_chat_usage_metadata(state, result)
    if _is_provider_context_overflow_result(result) and _can_retry_provider_context_overflow(state):
        stream_buffer.discard()
        _record_provider_context_overflow(state, result)
        retry_context = _rebuild_context_after_provider_overflow(graph_state, context)
        retry_request, stream_buffer = _with_buffered_response_stream(
            _build_native_tool_chat_request(retry_context, state),
            graph_state,
            source="assistant_langgraph_answer",
        )
        result = _run_chat_turn(graph_state, chat_adapter, retry_request)
        _record_chat_usage_metadata(state, result)
        context = retry_context
        if _is_provider_context_overflow_result(result):
            stream_buffer.discard()
            _record_provider_context_overflow(state, result, retry_failed=True)
            return _provider_context_overflow_final_answer(result), context
    if result.success and result.tool_calls:
        stream_buffer.discard()
        if not _selected_native_tool_specs(context):
            state.request.metadata["tool_call_returned_after_budget_exhaustion"] = True
            return _max_iteration_final_answer(context.max_iterations), context
        pending_decisions = [
            native_tool_call_to_assistant_decision(call)
            for call in result.tool_calls
        ]
        _record_native_tool_calls(
            state,
            result,
            iteration=context.iterations,
        )
        graph_state["pending_tool_decisions"] = pending_decisions
        decision = pending_decisions[0]
    elif result.success:
        stream_buffer.flush()
        graph_state["pending_tool_decisions"] = []
        decision = _native_final_decision(result)
    else:
        stream_buffer.discard()
        graph_state["pending_tool_decisions"] = []
        decision = _native_final_decision(result)
    return decision, context


def _run_chat_turn(
    graph_state: AssistantLoopState,
    chat_adapter: ChatAdapter,
    request: ChatRequest,
) -> ChatResult:
    state = graph_state["state"]
    iteration = int(graph_state.get("assistant_iterations", 0)) + 1
    span_id = new_span_id()
    started_at = perf_counter()
    provider = _safe_provider_label(getattr(chat_adapter, "provider", None))
    model = _safe_provider_label(getattr(chat_adapter, "model", None))
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
            node_name=graph_state.get("current_node_name", "assistant_loop"),
            status="failed",
            provider=provider,
            model=model,
            latency_ms=wall_latency_ms,
            span_id=span_id,
            attributes={"iteration": iteration, "wall_latency_ms": wall_latency_ms},
            error={"code": "provider_call_failed", "message": sanitize_trace_value(str(exc))},
        )
        raise
    wall_latency_ms = _elapsed_ms(started_at)
    provider_latency_ms = result.latency_ms
    append_observability_event(
        graph_state.get("trace_store"),
        trace_id=graph_state.get("trace_id") or state.trace_id,
        run_id=state.run_id,
        user_id=state.user_id,
        session_id=state.session_id,
        canonical_event="llm.chat.finished",
        node_name=graph_state.get("current_node_name", "assistant_loop"),
        status="succeeded" if result.success else "failed",
        provider=result.provider,
        model=result.model,
        latency_ms=provider_latency_ms if provider_latency_ms is not None else wall_latency_ms,
        span_id=span_id,
        attributes={
            "iteration": iteration,
            "message_kind": result.message_kind,
            "finish_reason": result.finish_reason,
            "tool_call_count": len(result.tool_calls),
            "provider_latency_ms": provider_latency_ms,
            "wall_latency_ms": wall_latency_ms,
            "usage": normalize_provider_token_usage(result.usage),
        },
        error=_chat_result_trace_error(result),
    )
    return result


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


def _rebuild_context_after_provider_overflow(
    graph_state: AssistantLoopState,
    context: AssistantDecisionContext,
) -> AssistantDecisionContext:
    _project_request_context(graph_state, context.request)
    pack = build_traced_assistant_context_pack(
        trace_store=graph_state.get("trace_store"),
        trace_id=graph_state.get("trace_id"),
        node_name=graph_state.get("current_node_name", "assistant"),
        state=graph_state["state"],
        request=context.request,
        observations=context.tool_observations,
        tool_specs=context.tool_specs,
        iteration=context.iterations,
        max_iterations=context.max_iterations,
        memory_text=context.memory_text,
        context_compactor=graph_state.get("context_compactor"),
    )
    return AssistantDecisionContext(
        context_pack=pack,
        request=pack.request,
        memory_summaries=pack.memory_summaries,
        memory_text=pack.memory_text,
        tool_specs=pack.tool_specs,
        tool_observations=pack.observations,
        iterations=context.iterations,
        max_iterations=context.max_iterations,
        is_mock=context.is_mock,
    )


def _project_request_context(graph_state: AssistantLoopState, request: UserRequest) -> None:
    projector = graph_state.get("context_projector")
    if callable(projector):
        projector(request)


def _provider_context_overflow_final_answer(result: ChatResult) -> AssistantDecision:
    message = "模型上下文超出限制，已尝试压缩后重试一次但仍失败。请缩短输入或拆分任务后再试。"
    reason = "Provider context overflow persisted after one compaction retry."
    error_message = next((sanitize_trace_value(error.message) for error in result.errors), "")
    notes = ["provider_context_overflow", "provider_context_overflow_retry_failed"]
    if error_message:
        notes.append("provider_error_sanitized")
    return AssistantDecision(
        type="final_answer",
        message=message,
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


def _native_final_decision(result: ChatResult) -> AssistantDecision:
    """Convert a native provider non-tool response into an internal terminal decision."""

    no_answer_code = next(
        (error.code for error in result.errors if error.code in MAIN_LLM_NO_ANSWER_MESSAGES),
        None,
    )
    if no_answer_code is not None:
        return AssistantDecision(
            type="final_answer",
            message=MAIN_LLM_NO_ANSWER_MESSAGES[no_answer_code],
            reason=f"Main LLM returned {no_answer_code} without usable content.",
            safety_notes=[no_answer_code],
        )
    if result.refusal:
        return AssistantDecision(
            type="final_answer",
            message=result.refusal,
            reason=_native_finish_reason(result, fallback="Provider returned a refusal instead of a tool call."),
            safety_notes=["provider_refusal"],
        )
    raw_output = result.response_text.strip()
    if raw_output:
        return AssistantDecision(
            type="final_answer",
            message=raw_output,
            reason=_native_finish_reason(result, fallback="Provider finished without requesting another tool."),
        )
    return AssistantDecision(
        type="final_answer",
        message="模型没有返回可用回答。",
        reason=_native_finish_reason(result, fallback="Provider finished without content or tool calls."),
        safety_notes=["empty_native_final_answer"],
    )


def _native_finish_reason(result: ChatResult, *, fallback: str) -> str:
    if result.finish_reason:
        return f"{fallback} finish_reason={result.finish_reason}."
    if result.message_kind:
        return f"{fallback} message_kind={result.message_kind}."
    return fallback


def _build_native_tool_chat_request(context: AssistantDecisionContext, state: AgentState) -> ChatRequest:
    return _compile_native_tool_chat_request(context, state).chat_request


def _compile_native_tool_chat_request(
    context: AssistantDecisionContext,
    state: AgentState,
) -> PromptCompileResult:
    compilation = PromptCompiler().compile(
        PromptCompileRequest(
            user_id=state.user_id,
            session_id=state.session_id,
            mode=PromptCompileMode.NATIVE_TOOL,
            user_query_fallback="native_tools assistant turn",
            profile=SystemPromptProfile.TEXT_DEFAULT,
            options=SystemPromptOptions(product_mode=True),
            context_pack=context.context_pack,
            observations=tuple(context.tool_observations),
            native_calls=tuple(_native_tool_calls_from_metadata(state)),
            tool_call_id_prefix="call_",
        )
    )
    if compilation.selected_tool_specs:
        return compilation
    return PromptCompileResult(
        chat_request=compilation.chat_request.model_copy(update={"tool_choice": None}),
        system_instruction=compilation.system_instruction,
        rendered_context=compilation.rendered_context,
        selected_tool_specs=compilation.selected_tool_specs,
    )


def _selected_native_tool_specs(context: AssistantDecisionContext) -> list[ToolSpec]:
    """Return the provider tool schemas selected for this prompt."""

    return list(
        prompt_tool_specs_for_mode(
            context.context_pack,
            PromptCompileMode.NATIVE_TOOL,
        )
    )


def _build_native_tool_messages(context: AssistantDecisionContext, state: AgentState) -> list[dict[str, Any]]:
    return _compile_native_tool_chat_request(context, state).chat_request.messages


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
    decision: AssistantDecision,
    context: AssistantDecisionContext,
) -> AssistantDecision:
    """Apply loop/safety guards after a policy proposes an assistant decision."""

    state = graph_state["state"]
    if decision.reason == "Empty or whitespace-only output.":
        guard = LoopGuard(state.request.metadata).record_empty_decision()
        if guard.triggered:
            _record_loop_guard(graph_state, guard)
            return _guard_final_answer(guard)
    if (
        decision.type == "tool_call"
        and decision.tool_name
        and not _is_plan_mode_active(state)
        and LoopGuard(state.request.metadata).terminal_tool_already_succeeded(decision.tool_name)
    ):
        guard = LoopGuardDecision(
            True,
            "duplicate_terminal_tool",
            f"{decision.tool_name} already succeeded in this run; answering with the existing result instead of calling it again.",
        )
        _record_loop_guard(graph_state, guard)
        return AssistantDecision(
            type="final_answer",
            message=None,
            reason=guard.message,
            safety_notes=[guard.code],
        )
    return decision


def _apply_memory_tool_selection_policy(
    graph_state: AssistantLoopState,
    decision: AssistantDecision,
    context: AssistantDecisionContext,
) -> AssistantDecision:
    """Record LLM-first memory tool selection without local semantic override."""

    audit = build_memory_tool_selection_audit(
        request=context.request,
        decision=decision,
        state=graph_state["state"],
        iteration=context.iterations,
        max_iterations=context.max_iterations,
        is_mock=context.is_mock,
    )
    record_memory_tool_selection_audit(context.request, audit)
    return decision


def _apply_terminal_decision(
    graph_state: AssistantLoopState,
    decision: AssistantDecision,
    context: AssistantDecisionContext,
) -> None:
    """Persist response state when the assistant decides to stop the loop."""

    if decision.type not in ("final_answer", "ask_followup"):
        return

    state = graph_state["state"]
    if _is_plan_mode_active(state):
        _mark_plan_mode_status(state, "completed")
    if decision.type == "ask_followup" or not state.tool_results:
        if _is_direct_chat_state(state):
            _set_direct_chat_response(graph_state, decision, context.iterations, context.tool_observations)
            return
        state.set_response(
            AgentResponse(
                message=decision.message or "已处理请求。",
                data={
                    "intent": state.intent.intent if state.intent else None,
                    "assistant_decision": decision.type,
                    "reason": decision.reason,
                    "iterations": context.iterations,
                    "tool_observations": len(context.tool_observations),
                    "plan_status": state.plan_status,
                    "current_step_id": state.current_step_id,
                    "plan_revision_count": state.plan_revision_count,
                    **_fallback_response_data(decision),
                },
                followup_question=decision.message if decision.type == "ask_followup" else None,
            )
        )
        state.status = "completed"
        return

    if _should_preserve_assistant_final_answer(decision=decision, is_mock=context.is_mock):
        _set_assistant_final_answer_response(graph_state, decision, context.iterations, context.tool_observations)
        state.status = "completed"
    elif state.status != "failed":
        state.status = "completed"


def _fallback_response_data(decision: AssistantDecision) -> dict[str, Any]:
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


def _max_iteration_final_answer(max_iterations: int) -> AssistantDecision:
    return AssistantDecision(
        type="final_answer",
        message=f"已达到最大工具调用次数 ({max_iterations})，这是我能提供的最好回答。",
        reason="安全限制：防止无限工具调用循环",
    )


def _mock_assistant_decision_from_plan(
    *,
    request: UserRequest,
    tool_observations: list[dict[str, Any]],
    state: AgentState,
    outputs_by_step: dict[str, ToolResult],
) -> AssistantDecision:
    """Return the next deterministic ReAct decision from the rule-based plan."""

    if state.plan is None:
        return AssistantDecision(
            type="final_answer",
            message="离线计划不可用，无法选择下一步工具。",
            reason="_decide_with_mock_plan(...) requires state.plan from the rule router.",
            safety_notes=["missing_rule_plan"],
        )

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
        from assistant_agent.agent.tool_input_builder import build_tool_input

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
                "plan_status": state.plan_status,
                "current_step_id": state.current_step_id,
                "plan_revision_count": state.plan_revision_count,
            },
            output_refs=[result.output_ref] if result.output_ref else [],
        )
    )


def _should_preserve_assistant_final_answer(*, decision: AssistantDecision, is_mock: bool) -> bool:
    """Return true when an assistant final answer should bypass response composition."""

    return (
        decision.type == "final_answer"
        and bool(decision.message)
        and not is_mock
        and not _assistant_final_answer_is_technical_failure(decision)
    )


def _assistant_final_answer_is_technical_failure(decision: AssistantDecision) -> bool:
    """Detect provider self-repair/parsing messages that should not face users."""

    message = decision.message or ""
    technical_messages = (
        "原始输出格式不完整",
        "无法正常解析",
        "助手决策输出格式不完整",
    )
    return any(marker in message for marker in technical_messages)


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
    return request.model_copy(update={"stream_callback": callback})


def _with_buffered_response_stream(
    request: ChatRequest,
    graph_state: AssistantLoopState,
    *,
    source: str,
) -> tuple[ChatRequest, _ResponseDeltaBuffer]:
    buffer = _ResponseDeltaBuffer(graph_state, source=source)
    if not buffer.enabled:
        return request, buffer
    return request.model_copy(update={"stream_callback": buffer.emit_delta}), buffer


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
        event = stream_delta_to_agent_event(
            text,
            payload,
            session_id=state.session_id,
            run_id=state.run_id,
            source=source,
        )
        if event is None:
            return
        event_sink.emit(event)

    return emit_delta


def _is_mock_chat_adapter(chat_adapter: ChatAdapter) -> bool:
    return getattr(chat_adapter, "provider", "") == "mock" or hasattr(chat_adapter, "MockChatAdapter")


def apply_plan_mode_transition_node(graph_state: AssistantLoopState) -> AssistantLoopState:
    """Apply enter/exit plan-mode decisions without executing tools."""

    decision = graph_state.get("assistant_decision")
    if decision is None or decision.type not in {"enter_plan_mode", "exit_plan_mode"}:
        return graph_state
    if decision.type == "enter_plan_mode":
        return _enter_plan_mode(graph_state, decision)
    return _exit_plan_mode(graph_state, decision)


def _enter_plan_mode(graph_state: AssistantLoopState, decision: AssistantDecision) -> AssistantLoopState:
    state = graph_state["state"]
    plan = decision.plan
    if plan is None:
        _fail_plan_mode_transition(
            graph_state,
            PlanValidationResult(
                accepted=False,
                code="planner_output_invalid",
                message="enter_plan_mode must include a valid TaskPlan in the plan field.",
            ),
        )
        return graph_state

    if state.plan is not None and state.plan_revision_count >= int(graph_state.get("max_plan_revisions", MAX_PLAN_REVISIONS)):
        _fail_plan_mode_transition(
            graph_state,
            PlanValidationResult(
                accepted=False,
                code="plan_revision_limit_exceeded",
                message=f"Plan revision limit reached ({graph_state.get('max_plan_revisions', MAX_PLAN_REVISIONS)}).",
            ),
        )
        return graph_state

    validation = PlanValidator(max_steps=int(graph_state.get("max_plan_steps", MAX_PLAN_STEPS))).validate(
        plan,
        graph_state["tool_executor"].registry,
    )
    state.request.metadata["last_plan_validation"] = validation.model_dump(mode="json")
    if not validation.accepted:
        _fail_plan_mode_transition(graph_state, validation)
        return graph_state

    if state.plan is not None:
        state.plan_revision_count += 1
    state.set_plan(plan)
    state.current_step_id = None if plan.requires_followup else _next_pending_plan_step_id(plan, graph_state["outputs_by_step"])
    _mark_plan_mode_status(state, "completed" if plan.requires_followup else "active")
    _record_plan_mode_transition(
        graph_state,
        decision_type="enter_plan_mode",
        reason=decision.reason or validation.message,
        plan=plan,
        validation=validation,
    )
    if plan.requires_followup:
        state.set_response(
            AgentResponse(
                message=plan.followup_question or "请补充必要信息后我再继续。",
                data={
                    "assistant_decision": "ask_followup",
                    "plan_status": state.plan_status,
                    "plan_revision_count": state.plan_revision_count,
                    "plan_validation": validation.model_dump(mode="json"),
                },
                followup_question=plan.followup_question,
            )
        )
    return graph_state


def _exit_plan_mode(graph_state: AssistantLoopState, decision: AssistantDecision) -> AssistantLoopState:
    state = graph_state["state"]
    state.current_step_id = None
    _mark_plan_mode_status(state, "completed")
    _record_plan_mode_transition(
        graph_state,
        decision_type="exit_plan_mode",
        reason=decision.reason or "Assistant exited plan mode.",
        plan=state.plan,
    )
    next_action = decision.next_action or "continue"
    if next_action in {"final_answer", "ask_followup"}:
        output_refs = [result.output_ref for result in state.tool_results if result.output_ref]
        state.set_response(
            AgentResponse(
                message=decision.message or "已处理请求。",
                data={
                    "final_answer_source": "assistant_loop",
                    "assistant_decision": next_action,
                    "reason": decision.reason,
                    "plan_status": state.plan_status,
                    "plan_revision_count": state.plan_revision_count,
                    "tool_count": len(state.tool_calls),
                    "tool_observations": len(graph_state.get("tool_observations", [])),
                    "output_refs": output_refs,
                },
                followup_question=decision.message if next_action == "ask_followup" else None,
                output_refs=output_refs,
            )
        )
    return graph_state


def _fail_plan_mode_transition(graph_state: AssistantLoopState, validation: PlanValidationResult) -> None:
    state = graph_state["state"]
    _mark_plan_mode_status(state, "failed")
    state.errors.append(
        AgentError(
            message=validation.message,
            source="plan_mode",
            details={"code": validation.code, "recovery_action": "stop_with_error"},
        )
    )
    state.request.metadata["last_plan_validation"] = validation.model_dump(mode="json")
    state.set_response(
        AgentResponse(
            message=f"计划无效：{validation.message}",
            data={
                "assistant_decision": "final_answer",
                "plan_status": state.plan_status,
                "plan_validation": validation.model_dump(mode="json"),
            },
        )
    )
    _record_plan_mode_transition(
        graph_state,
        decision_type="plan_rejected",
        reason=validation.message,
        validation=validation,
    )
    _append_trace(
        graph_state,
        event_type="assistant_decision",
        status="plan_rejected",
        output_summary={"plan_validation": validation.model_dump(mode="json")},
        error={"code": validation.code, "message": validation.message},
    )


def execute_requested_tool_node(graph_state: AssistantLoopState) -> AssistantLoopState:
    """Execute the current provider-native tool batch within the run budget."""

    pending = list(graph_state.get("pending_tool_decisions") or [])
    if not pending:
        decision = graph_state.get("assistant_decision")
        pending = [decision] if decision is not None and decision.type == "tool_call" else []
    if not pending:
        return graph_state

    current = graph_state
    max_tool_calls = int(graph_state.get("max_tool_iterations", _get_max_tool_iterations()))
    tool_calls_used = int(graph_state.get("tool_calls_used", 0))
    for index, decision in enumerate(pending):
        if tool_calls_used >= max_tool_calls:
            skipped = len(pending) - index
            metadata = current["state"].request.metadata
            metadata["tool_call_budget_exhausted"] = True
            metadata["tool_calls_skipped_for_budget"] = (
                int(metadata.get("tool_calls_skipped_for_budget", 0)) + skipped
            )
            break
        current = _execute_single_requested_tool_node(
            {
                **current,
                "assistant_decision": decision,
            }
        )
        tool_calls_used += 1
        current["tool_calls_used"] = tool_calls_used
        if current["state"].status in {"failed", "cancelled"}:
            break

    current["pending_tool_decisions"] = []
    return current


def _execute_single_requested_tool_node(graph_state: AssistantLoopState) -> AssistantLoopState:
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
    step, plan_rejection = _current_plan_step(state, decision, graph_state["outputs_by_step"])
    if plan_rejection is not None:
        state.errors.append(
            AgentError(
                message=plan_rejection.error_message or plan_rejection.summary,
                source=tool_name or "plan_mode",
                details={"code": plan_rejection.error_code or "plan_step_rejected", "recovery_action": "revise_plan"},
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
            validated_input=validation.validated_input,
        )

        observation = observation_from_tool_result(
            result,
            request_text=graph_state["request"].text,
            prior_observations=tool_observations,
        )
        if _is_plan_mode_active(state) and not result.success and state.status == "failed":
            state.status = "running"
            _mark_plan_mode_status(state, "replanning")
        if result.success:
            LoopGuard(state.request.metadata).record_terminal_tool_success(tool_name)
        guard = (
            LoopGuardDecision(False, "ok", "Guard not triggered for optional step.")
            if step is not None and step.optional
            else LoopGuard(state.request.metadata).record_tool_result(tool_name=tool_name, success=result.success)
        )
        if guard.triggered:
            _record_loop_guard(graph_state, guard)
            if _is_plan_mode_active(state):
                pass
            elif state.status != "failed":
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

        outputs_by_step = {
            **graph_state["outputs_by_step"],
            step_id: result,
        }
        if step is not None:
            _advance_plan_after_tool_result(state, outputs_by_step, result)
        return {
            **graph_state,
            "tool_observations": _record_react_observation(graph_state, tool_observations, observation),
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
            error_code="tool_exception",
            error_message=str(e),
        )
        return {
            **graph_state,
            "tool_observations": _record_react_observation(graph_state, tool_observations, observation),
        }


def _current_plan_step(
    state: AgentState,
    decision: AssistantDecision,
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
                error_code="unknown_step",
                error_message=f"Unknown plan step: {decision.step_id}.",
                next_step_hint="Use an existing plan step id or revise the plan with enter_plan_mode.",
            )
    else:
        step = _next_matching_plan_step(state.plan, decision.tool_name, outputs_by_step)
        if step is None:
            return None, rejected_observation(
                tool_name=decision.tool_name or "unknown",
                error_code="plan_step_not_found",
                error_message=f"Tool {decision.tool_name or 'unknown'} is not part of the active plan.",
                next_step_hint="Revise the plan before calling a tool that is not in it.",
            )

    if step.tool_name is None:
        return None, rejected_observation(
            tool_name=decision.tool_name or "unknown",
            error_code="non_executable_step",
            error_message=f"Plan step {step.step_id} has no executable tool.",
            next_step_hint="Revise the plan or answer directly.",
        )
    if step.tool_name != decision.tool_name:
        return None, rejected_observation(
            tool_name=decision.tool_name or "unknown",
            error_code="plan_tool_mismatch",
            error_message=f"Plan step {step.step_id} requires {step.tool_name}, not {decision.tool_name}.",
            next_step_hint="Call the planned tool or revise the plan.",
        )
    dependency_error = _dependency_error(step, outputs_by_step)
    if dependency_error is not None:
        return None, rejected_observation(
            tool_name=step.tool_name,
            error_code="dependency_not_satisfied",
            error_message=dependency_error,
            next_step_hint="Execute dependency steps first or revise the plan.",
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


def _record_plan_mode_transition(
    graph_state: AssistantLoopState,
    *,
    decision_type: str,
    reason: str,
    plan: TaskPlan | None = None,
    validation: PlanValidationResult | None = None,
) -> None:
    state = graph_state["state"]
    steps = state.request.metadata.setdefault("assistant_loop_steps", [])
    if isinstance(steps, list):
        steps.append(
            {
                "iteration": len(steps) + 1,
                "decision_type": decision_type,
                "reason": reason,
                "plan_step_count": len(plan.steps) if plan is not None else 0,
                "plan_status": state.plan_status,
                "step_id": state.current_step_id,
            }
        )
    trace_event = {
        "iteration": len(state.request.metadata.get("decision_trace", [])) + 1,
        "event": "decision",
        "decision_type": decision_type,
        "decision_summary": reason,
        "plan_step_count": len(plan.steps) if plan is not None else 0,
        "step_id": state.current_step_id,
        "plan_status": state.plan_status,
    }
    decision_trace = state.request.metadata.setdefault("decision_trace", [])
    if isinstance(decision_trace, list):
        decision_trace.append(trace_event)
    _emit_agent_trace_event(graph_state, trace_event)
    _append_trace(
        graph_state,
        event_type="assistant_decision",
        status=decision_type,
        output_summary={
            "reason": reason,
            "plan_step_count": len(plan.steps) if plan is not None else 0,
            "plan_status": state.plan_status,
            "plan_validation": validation.model_dump(mode="json") if validation is not None else None,
        },
        error={"code": validation.code, "message": validation.message} if validation is not None and not validation.accepted else None,
    )


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

    Returns "execute_tool", "apply_plan_mode_transition", or "finish".
    """
    state = graph_state["state"]
    decision = graph_state.get("assistant_decision")

    if state.status == "failed":
        return "finish"

    if state.status == "completed":
        return "finish"

    if decision is None:
        return "finish"

    if decision.type in {"enter_plan_mode", "exit_plan_mode"}:
        return "apply_plan_mode_transition"

    if decision.type == "tool_call":
        if not decision.tool_name:
            return "finish"
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
    decision: AssistantDecision,
    iteration: int,
    *,
    context: AssistantDecisionContext | None = None,
) -> None:
    """Keep compact decision trace data for local demo inspection."""

    state = graph_state["state"]
    steps = state.request.metadata.setdefault("assistant_loop_steps", [])
    if not isinstance(steps, list):
        steps = []
        state.request.metadata["assistant_loop_steps"] = steps
    trace_event = _decision_trace_event(decision, iteration)
    memory_selection = _memory_tool_selection_trace(state.request.metadata)
    decision_trace = state.request.metadata.setdefault("decision_trace", [])
    if isinstance(decision_trace, list):
        decision_trace.append(trace_event)
    steps.append(
        {
            "iteration": iteration + 1,
            "decision_type": decision.type,
            "tool_name": decision.tool_name,
            "tool_input": decision.tool_input or {},
            "step_id": decision.step_id,
            "message": decision.message,
            "reason": decision.reason,
            "decision_summary": decision.reason,
            "confidence": decision.confidence,
            "safety_notes": decision.safety_notes,
            "plan_step_count": len(decision.plan.steps) if decision.plan is not None else (len(state.plan.steps) if state.plan is not None else 0),
            "plan_status": state.plan_status,
            "memory_tool_selection": memory_selection,
        }
    )
    _emit_agent_trace_event(graph_state, trace_event)
    context_summary = _context_trace_summary(context)
    output_summary = {
        "decision_type": decision.type,
        "reason": decision.reason,
        "confidence": decision.confidence,
        "message_present": bool(decision.message),
        "step_id": decision.step_id,
        "plan_status": state.plan_status,
        "plan_step_count": len(decision.plan.steps) if decision.plan is not None else (len(state.plan.steps) if state.plan is not None else 0),
    }
    if context_summary:
        output_summary["context"] = context_summary
        output_summary["context_report_v1"] = _context_report_summary(context)
    if memory_selection:
        output_summary["memory_tool_selection"] = memory_selection
    _append_trace(
        graph_state,
        event_type="assistant_decision",
        canonical_event="react.decision",
        status=decision.type,
        tool_name=decision.tool_name,
        output_summary=output_summary,
        attributes={
            "iteration": iteration + 1,
            "decision_type": decision.type,
            "tool_name": decision.tool_name,
            "step_id": decision.step_id,
            "plan_status": state.plan_status,
            "safety_notes": decision.safety_notes,
        },
    )


def _context_trace_summary(context: AssistantDecisionContext | None) -> dict[str, Any]:
    if context is None:
        return {}
    pack = context.context_pack
    return {
        "context_schema_version": "context_observability_v1",
        "budget": pack.budget.model_dump(mode="json"),
        "source_counts": pack.source_counts,
        "compaction": _context_compaction_summary(pack.observations),
        "tool_catalog": pack.tool_catalog_summary.model_dump(mode="json"),
        "context_sources": pack.context_source_report.model_dump(mode="json"),
        "compactor_type": pack.compactor_type,
        "context_summary_present": pack.context_summary is not None,
        "memory_promotion_candidates": _metadata_int(pack.request.metadata, "memory_promotion_candidates"),
        "memory_promotion_written": _metadata_int(pack.request.metadata, "memory_promotion_written"),
        "memory_tool_selection": _memory_tool_selection_trace(pack.request.metadata),
    }


def _context_report_summary(context: AssistantDecisionContext) -> dict[str, Any]:
    system_prompt = render_system_instruction(
        SystemPromptProfile.TEXT_DEFAULT,
        options=SystemPromptOptions(product_mode=True),
        owner_persona=owner_persona_for_pack(context.context_pack),
    )
    return build_context_report(
        context.context_pack,
        system_prompt=system_prompt,
        selected_tool_specs=_selected_native_tool_specs(context),
    ).model_dump(mode="json")


def _memory_tool_selection_trace(metadata: dict[str, Any]) -> dict[str, Any]:
    selection = metadata.get("memory_tool_selection")
    if not isinstance(selection, dict):
        return {}
    return {
        "strategy": selection.get("strategy"),
        "action": selection.get("action"),
        "selected_memory_tool": selection.get("selected_memory_tool"),
        "keyword_signals": selection.get("keyword_signals", []),
        "missed_signals": selection.get("missed_signals", []),
        "candidate_mode": selection.get("candidate_mode"),
        "auto_write": selection.get("auto_write"),
        "vector_shadow_hit_count": _selection_vector_hit_count(selection),
    }


def _selection_vector_hit_count(selection: dict[str, Any]) -> int:
    signal = selection.get("vector_shadow_signal")
    if not isinstance(signal, dict):
        return 0
    value = signal.get("hit_count")
    return value if isinstance(value, int) and value >= 0 else 0


def _context_compaction_summary(observations: list[dict[str, Any]]) -> dict[str, int]:
    compacted_count = 0
    original_chars = 0
    compacted_chars = 0
    pruned_payload_keys = 0
    command_outputs_truncated = 0
    original_command_output_chars = 0
    compacted_command_output_chars = 0
    for observation in observations:
        compaction = observation.get("compaction")
        if not isinstance(compaction, dict):
            continue
        compacted_count += 1
        original = compaction.get("original_chars")
        compacted = compaction.get("compacted_chars")
        if isinstance(original, int):
            original_chars += original
        if isinstance(compacted, int):
            compacted_chars += compacted
        pruned_keys = compaction.get("pruned_keys")
        if isinstance(pruned_keys, list):
            pruned_payload_keys += len(pruned_keys)
        omitted_pruned_keys = compaction.get("omitted_pruned_keys_count")
        if isinstance(omitted_pruned_keys, int):
            pruned_payload_keys += omitted_pruned_keys
        command_output_keys = compaction.get("command_output_keys")
        if isinstance(command_output_keys, list):
            command_outputs_truncated += len(command_output_keys)
        omitted_command_output_keys = compaction.get("omitted_command_output_keys_count")
        if isinstance(omitted_command_output_keys, int):
            command_outputs_truncated += omitted_command_output_keys
        command_original = compaction.get("original_command_output_chars")
        command_compacted = compaction.get("compacted_command_output_chars")
        if isinstance(command_original, int):
            original_command_output_chars += command_original
        if isinstance(command_compacted, int):
            compacted_command_output_chars += command_compacted
    return {
        "compacted_observations": compacted_count,
        "original_observation_chars": original_chars,
        "compacted_observation_chars": compacted_chars,
        "pruned_payload_keys": pruned_payload_keys,
        "command_outputs_truncated": command_outputs_truncated,
        "original_command_output_chars": original_command_output_chars,
        "compacted_command_output_chars": compacted_command_output_chars,
    }


def _metadata_int(metadata: dict[str, Any], key: str) -> int:
    value = metadata.get(key)
    return value if isinstance(value, int) and value >= 0 else 0


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
        canonical_event="tool.observation",
        status=payload.get("status"),
        tool_name=payload.get("tool_name"),
        output_summary={
            "summary": payload.get("summary"),
            "output_ref": payload.get("output_ref"),
            "next_step_hint": payload.get("next_step_hint"),
        },
        attributes={
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
        if decision.step_id:
            payload["step_id"] = decision.step_id
    if decision.type in {"enter_plan_mode", "exit_plan_mode"}:
        payload["step_id"] = decision.step_id
        payload["plan_step_count"] = len(decision.plan.steps) if decision.plan is not None else 0
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
        canonical_event="loop_guard.triggered",
        status="triggered",
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
