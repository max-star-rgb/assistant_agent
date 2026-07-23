"""Tool execution service used by workflows and LangGraph nodes."""

from dataclasses import dataclass
from time import monotonic, perf_counter, sleep
from typing import Any, Literal

from pydantic import BaseModel

from assistant_agent.agent.cancellation import (
    AgentRunCancelled,
    CANCELLATION_ERROR_CODE,
    DEFAULT_CANCELLATION_MESSAGE,
    raise_if_cancelled,
)
from assistant_agent.agent.legacy_tool_mapping import (
    canonical_capability_for_action,
    canonical_capability_for_tool,
)
from assistant_agent.agent.state import AgentState
from assistant_agent.agent.recovery import RecoveryPolicy, ToolFailureMode, classify_error
from assistant_agent.schemas.api import api_error
from assistant_agent.schemas.capability_output import contract_summary
from assistant_agent.schemas.events import AgentEvent
from assistant_agent.schemas.durable_tasks import TrustedTaskBinding
from assistant_agent.schemas.identity import RequestIdentity
from assistant_agent.schemas.planning import TaskStep
from assistant_agent.schemas.realtime_cancellation import build_realtime_turn_cancellation_metadata
from assistant_agent.schemas.tools import ToolResult, ToolSpec
from assistant_agent.services.event_sink import EventSink
from assistant_agent.services.provider_errors import sanitize_error_detail, sanitize_error_message
from assistant_agent.services.provider_policy import ProviderExecutionPolicy
from assistant_agent.services.agent_service_entry import is_trusted_agent_service_request
from assistant_agent.services.tool_call_boundary import (
    build_post_tool_call_summary,
    build_pre_tool_call_summary,
)
from assistant_agent.schemas.tool_ids import (
    IMAGE_GENERATION_CAPABILITY,
    IMAGE_GENERATION_TOOL_NAME,
    MEMORY_RETRIEVAL_CAPABILITY,
    MEMORY_RETRIEVAL_TOOL_NAME,
    MEMORY_SAVE_CAPABILITY,
    MEMORY_SAVE_TOOL_NAME,
)
from assistant_agent.services.trace_store import TraceEvent, TraceStore, sanitize_trace_value
from assistant_agent.services.trace_content_policy import local_trace_content_enabled
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.registry import ToolRegistry, create_default_registry


@dataclass(frozen=True)
class PreparedToolCall:
    """Serially prepared call whose invocation no longer needs mutable AgentState."""

    step_id: str
    tool_name: str
    tool_input: dict[str, Any]
    invocation_input: BaseModel | dict[str, Any]
    step: TaskStep | None
    call_id: str
    capability: str
    tool_spec: ToolSpec
    started_at: float
    tool_span_id: str
    context: ToolContext | None
    disposition: Literal["invoke", "confirmation"]
    prepared_result: ToolResult | None
    trace_store: TraceStore | None
    trace_id: str | None
    node_name: str
    failure_mode: ToolFailureMode


@dataclass(frozen=True)
class ToolInvocationResult:
    """Tool-body outcome awaiting ordered state/trace commit."""

    result: ToolResult
    retry_count: int = 0
    cancellation: AgentRunCancelled | None = None
    cancellation_metadata: dict[str, Any] | None = None


class ToolExecutor:
    """Run tools through the registry and update AgentState records."""

    def __init__(
        self,
        registry: ToolRegistry | None = None,
        event_sink: EventSink | None = None,
        recovery_policy: RecoveryPolicy | None = None,
        execution_policy: ProviderExecutionPolicy | None = None,
        context_metadata: dict[str, Any] | None = None,
        cancel_token: Any | None = None,
    ) -> None:
        self.registry = registry or create_default_registry()
        self.event_sink = event_sink
        self.recovery_policy = recovery_policy or RecoveryPolicy()
        self.execution_policy = execution_policy or ProviderExecutionPolicy.from_env()
        self.context_metadata = dict(context_metadata or {})
        self.cancel_token = cancel_token

    def run_tool(
        self,
        state: AgentState,
        step_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
        step: TaskStep | None = None,
        trace_store: TraceStore | None = None,
        trace_id: str | None = None,
        node_name: str | None = None,
        validated_input: BaseModel | None = None,
        failure_mode: ToolFailureMode = "stop_run",
    ) -> ToolResult:
        """Run one governed tool through serial prepare/invoke/commit stages."""

        prepared = self.prepare_tool_call(
            state,
            step_id,
            tool_name,
            tool_input,
            step=step,
            trace_store=trace_store,
            trace_id=trace_id,
            node_name=node_name,
            validated_input=validated_input,
            failure_mode=failure_mode,
        )
        invocation = self.invoke_tool(prepared)
        return self.commit_tool_result(state, prepared, invocation)

    def prepare_tool_call(
        self,
        state: AgentState,
        step_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
        step: TaskStep | None = None,
        trace_store: TraceStore | None = None,
        trace_id: str | None = None,
        node_name: str | None = None,
        validated_input: BaseModel | None = None,
        failure_mode: ToolFailureMode = "stop_run",
    ) -> PreparedToolCall:
        """Prepare governance and lifecycle state in serial order."""

        effective_node_name = node_name or "tool_executor"
        raise_if_cancelled(
            self.cancel_token,
            phase="before_tool",
            node_name=effective_node_name,
            source="tool_executor",
            details={"tool_name": tool_name, "step_id": step_id},
            state=state,
        )
        if validated_input is not None and not isinstance(
            validated_input,
            self.registry.get(tool_name).input_schema,
        ):
            raise TypeError(f"validated_input does not match {tool_name} input_schema")
        normalized_input = (
            validated_input.model_dump(mode="python")
            if validated_input is not None
            else tool_input
        )
        tool = self.registry.get(tool_name)
        bound_input = _bind_runtime_identity(tool, normalized_input, state)
        bound_input = _bind_runtime_media_inputs(tool, bound_input, state)
        bound_input = _bind_durable_idempotency(
            bound_input,
            step_id=step_id,
            context_metadata=self.context_metadata,
        )
        tool_spec = self.registry.get_spec(tool_name)
        confirmed = _tool_confirmation_granted(state.request.metadata, tool_name)
        requires_confirmation = tool_spec.requires_confirmation and not confirmed
        execution_summary = {
            "category": tool_spec.category,
            "requires_confirmation": requires_confirmation,
            "confirmation_granted": confirmed,
        }
        pre_tool_call = build_pre_tool_call_summary(
            tool_name=tool_name,
            tool_input=bound_input,
            registry=self.registry,
            request=state.request,
            state=state,
            step_id=step_id,
            cancel_token=self.cancel_token,
            tool_contract=execution_summary,
        )
        call = state.add_tool_call(tool_name, bound_input)
        capability = _capability_name(tool_name, step)
        started_at = perf_counter()
        tool_span_id = f"span_{call.call_id}"
        _append_tool_trace_event(
            trace_store,
            trace_id=trace_id,
            state=state,
            node_name=effective_node_name,
            canonical_event="tool.started",
            status="started",
            capability=capability,
            tool_name=tool_name,
            call_id=call.call_id,
            step_id=step_id,
            span_id=tool_span_id,
            tool_contract=execution_summary,
            input_summary=_policy_safe_input_summary(bound_input),
        )
        self._emit(
            AgentEvent(
                type="tool_started",
                session_id=state.session_id,
                run_id=state.run_id,
                tool_name=tool_name,
                payload={
                    "call_id": call.call_id,
                    "step_id": step_id,
                    "pre_tool_call": pre_tool_call,
                },
            )
        )
        disposition: Literal["invoke", "confirmation"] = "invoke"
        prepared_result = None
        context = None
        if requires_confirmation:
            disposition = "confirmation"
            prepared_result = _confirmation_required_result(
                tool_name=tool_name,
                latency_ms=int((perf_counter() - started_at) * 1000),
            )
        else:
            before_tool_execution = self.context_metadata.get("_before_tool_execution")
            if callable(before_tool_execution):
                before_tool_execution()
            context_metadata = {
                **{
                    key: value
                    for key, value in self.context_metadata.items()
                    if key != "_before_tool_execution"
                },
                "request_text": state.request.text or "",
                "request_metadata": dict(state.request.metadata),
                "request_identity": RequestIdentity.from_user_request(
                    state.request
                ).model_dump(mode="json"),
            }
            context = ToolContext(
                run_id=state.run_id,
                user_id=state.user_id,
                session_id=state.session_id,
                metadata=context_metadata,
                cancel_token=self.cancel_token,
            )
        invocation_input: BaseModel | dict[str, Any] = bound_input
        if validated_input is not None:
            invocation_input = validated_input.model_copy(update=bound_input)
        return PreparedToolCall(
            step_id=step_id,
            tool_name=tool_name,
            tool_input=bound_input,
            invocation_input=invocation_input,
            step=step,
            call_id=call.call_id,
            capability=capability,
            tool_spec=tool_spec,
            started_at=started_at,
            tool_span_id=tool_span_id,
            context=context,
            disposition=disposition,
            prepared_result=prepared_result,
            trace_store=trace_store,
            trace_id=trace_id,
            node_name=effective_node_name,
            failure_mode=failure_mode,
        )

    def invoke_tool(self, prepared: PreparedToolCall) -> ToolInvocationResult:
        """Invoke a prepared tool body without mutating AgentState/history/trace."""

        if prepared.prepared_result is not None:
            return ToolInvocationResult(result=prepared.prepared_result)
        if prepared.context is None:
            raise RuntimeError("Prepared tool call is missing its ToolContext.")
        try:
            result, retry_count = self._run_with_retry(
                prepared.tool_name,
                prepared.invocation_input,
                prepared.context,
                step_id=prepared.step_id,
                preserve_success_after_cancel=_preserve_success_after_cancel(
                    prepared.tool_spec
                ),
                max_retries=_effective_max_retries(
                    tool_spec=prepared.tool_spec,
                    global_max_retries=self.execution_policy.retry.max_retries,
                ),
            )
            latency_ms = int((perf_counter() - prepared.started_at) * 1000)
            if result.latency_ms is None:
                result.latency_ms = latency_ms
            return ToolInvocationResult(result=result, retry_count=retry_count)
        except AgentRunCancelled as exc:
            latency_ms = int((perf_counter() - prepared.started_at) * 1000)
            error_details = build_realtime_turn_cancellation_metadata(
                {
                    **exc.details,
                    "step_id": prepared.step_id,
                    "tool_name": prepared.tool_name,
                    "retryable": False,
                },
                phase="tool_running",
            )
            return ToolInvocationResult(
                result=_cancelled_tool_result(
                    prepared.tool_name,
                    latency_ms=latency_ms,
                    cancel_metadata=error_details,
                ),
                cancellation=exc,
                cancellation_metadata=error_details,
            )

    def commit_tool_result(
        self,
        state: AgentState,
        prepared: PreparedToolCall,
        invocation: ToolInvocationResult,
    ) -> ToolResult:
        """Commit one invocation outcome to shared runtime stores in coordinator order."""

        if invocation.cancellation is not None:
            return self._commit_staged_cancellation(state, prepared, invocation)
        if prepared.disposition == "confirmation":
            return self._commit_staged_success(state, prepared, invocation)

        result = invocation.result
        if result.success:
            return self._commit_staged_success(state, prepared, invocation)
        return self._commit_staged_failure(state, prepared, invocation)

    def _commit_staged_success(
        self,
        state: AgentState,
        prepared: PreparedToolCall,
        invocation: ToolInvocationResult,
    ) -> ToolResult:
        result = invocation.result
        reported_latency_ms = result.latency_ms
        latency_ms = int((perf_counter() - prepared.started_at) * 1000)
        state.complete_tool_call(prepared.call_id, result)
        post_tool_call = build_post_tool_call_summary(
            tool_name=prepared.tool_name,
            result=result,
            state=state,
            step_id=prepared.step_id,
            call_id=prepared.call_id,
            latency_ms=latency_ms,
            retry_count=invocation.retry_count,
            tool_contract=_execution_summary(prepared.tool_spec, prepared.disposition),
            registry=self.registry,
        )
        event_payload = {
            "call_id": prepared.call_id,
            "step_id": prepared.step_id,
            "latency_ms": latency_ms,
            "retry_count": invocation.retry_count,
            "post_tool_call": post_tool_call,
        }
        if prepared.disposition == "invoke":
            event_payload["contract"] = contract_summary(result.contract)
        self._emit(
            AgentEvent(
                type="tool_finished",
                session_id=state.session_id,
                run_id=state.run_id,
                tool_name=prepared.tool_name,
                output_ref=result.output_ref,
                payload=event_payload,
            )
        )
        _append_tool_trace_event(
            prepared.trace_store,
            trace_id=prepared.trace_id,
            state=state,
            node_name=prepared.node_name,
            canonical_event="tool.finished",
            status=str(post_tool_call.get("status") or "succeeded"),
            capability=prepared.capability,
            tool_name=prepared.tool_name,
            call_id=prepared.call_id,
            step_id=prepared.step_id,
            span_id=prepared.tool_span_id,
            latency_ms=latency_ms,
            reported_latency_ms=reported_latency_ms,
            retry_count=invocation.retry_count,
            tool_contract=_execution_summary(prepared.tool_spec, prepared.disposition),
            input_summary=_policy_safe_input_summary(prepared.tool_input),
            output_summary={
                **_policy_safe_output_summary(result),
            },
            provider=_provider_name(result.data or {}),
            model=_model_name(result.data or {}),
        )
        return result

    def _commit_staged_failure(
        self,
        state: AgentState,
        prepared: PreparedToolCall,
        invocation: ToolInvocationResult,
    ) -> ToolResult:
        result = invocation.result
        reported_latency_ms = result.latency_ms
        latency_ms = int((perf_counter() - prepared.started_at) * 1000)
        decision = self.recovery_policy.decide(
            result,
            prepared.step,
            failure_mode=prepared.failure_mode,
        )
        result.error = decision.message
        post_tool_call = build_post_tool_call_summary(
            tool_name=prepared.tool_name,
            result=result,
            state=state,
            step_id=prepared.step_id,
            call_id=prepared.call_id,
            latency_ms=latency_ms,
            retry_count=invocation.retry_count,
            tool_contract=_execution_summary(prepared.tool_spec, prepared.disposition),
            registry=self.registry,
        )
        state.fail_tool_call(
            prepared.call_id,
            decision.message,
            result,
            error_details={
                "code": decision.error_code,
                "recovery_action": decision.action,
                "optional_step": decision.optional_step,
                "retryable": decision.retryable,
                "step_id": prepared.step_id,
                "retry_count": invocation.retry_count,
            },
            stop_run=decision.action == "stop_with_error",
        )
        self._emit(
            AgentEvent(
                type="tool_failed",
                session_id=state.session_id,
                run_id=state.run_id,
                tool_name=prepared.tool_name,
                error=api_error(
                    decision.error_code,
                    decision.message,
                    detail={"step_id": prepared.step_id, "recovery_action": decision.action},
                    recoverable=decision.retryable,
                ).model_dump(mode="json"),
                payload={
                    "call_id": prepared.call_id,
                    "step_id": prepared.step_id,
                    "latency_ms": latency_ms,
                    "retry_count": invocation.retry_count,
                    "code": decision.error_code,
                    "recovery_action": decision.action,
                    "contract": contract_summary(result.contract),
                    "post_tool_call": post_tool_call,
                },
            )
        )
        _append_tool_trace_event(
            prepared.trace_store,
            trace_id=prepared.trace_id,
            state=state,
            node_name=prepared.node_name,
            event_type="tool_failed",
            canonical_event="tool.failed",
            status="failed",
            capability=prepared.capability,
            tool_name=prepared.tool_name,
            call_id=prepared.call_id,
            step_id=prepared.step_id,
            span_id=prepared.tool_span_id,
            latency_ms=latency_ms,
            reported_latency_ms=reported_latency_ms,
            retry_count=invocation.retry_count,
            tool_contract=_execution_summary(prepared.tool_spec, prepared.disposition),
            input_summary=_policy_safe_input_summary(prepared.tool_input),
            output_summary={
                **_policy_safe_output_summary(result),
            },
            provider=_provider_name(result.data or {}),
            model=_model_name(result.data or {}),
            error_code=decision.error_code,
            error_message=_policy_safe_error(decision.message),
            recovery_action=decision.action,
        )
        return result

    def _commit_staged_cancellation(
        self,
        state: AgentState,
        prepared: PreparedToolCall,
        invocation: ToolInvocationResult,
    ) -> ToolResult:
        result = invocation.result
        error_details = invocation.cancellation_metadata or {}
        reported_latency_ms = result.latency_ms
        latency_ms = int((perf_counter() - prepared.started_at) * 1000)
        post_tool_call = build_post_tool_call_summary(
            tool_name=prepared.tool_name,
            result=result,
            state=state,
            step_id=prepared.step_id,
            call_id=prepared.call_id,
            latency_ms=latency_ms,
            retry_count=0,
            cancel_metadata=error_details,
            tool_contract=_execution_summary(prepared.tool_spec, prepared.disposition),
            registry=self.registry,
        )
        state.fail_tool_call(
            prepared.call_id,
            DEFAULT_CANCELLATION_MESSAGE,
            result,
            error_details=error_details,
            stop_run=True,
        )
        self._emit(
            AgentEvent(
                type="tool_failed",
                session_id=state.session_id,
                run_id=state.run_id,
                tool_name=prepared.tool_name,
                error=api_error(
                    CANCELLATION_ERROR_CODE,
                    DEFAULT_CANCELLATION_MESSAGE,
                    detail={"step_id": prepared.step_id, "source": prepared.tool_name},
                    recoverable=False,
                ).model_dump(mode="json"),
                payload={
                    "call_id": prepared.call_id,
                    "step_id": prepared.step_id,
                    "latency_ms": latency_ms,
                    "retry_count": 0,
                    "code": CANCELLATION_ERROR_CODE,
                    "post_tool_call": post_tool_call,
                },
            )
        )
        _append_tool_trace_event(
            prepared.trace_store,
            trace_id=prepared.trace_id,
            state=state,
            node_name=prepared.node_name,
            event_type="tool_failed",
            canonical_event="tool.failed",
            status="failed",
            capability=prepared.capability,
            tool_name=prepared.tool_name,
            call_id=prepared.call_id,
            step_id=prepared.step_id,
            span_id=prepared.tool_span_id,
            latency_ms=latency_ms,
            reported_latency_ms=reported_latency_ms,
            retry_count=0,
            tool_contract=_execution_summary(prepared.tool_spec, prepared.disposition),
            input_summary=_policy_safe_input_summary(prepared.tool_input),
            output_summary={"cancelled": True},
            error_code=CANCELLATION_ERROR_CODE,
            error_message=DEFAULT_CANCELLATION_MESSAGE,
            recovery_action="cancelled",
        )
        exc = invocation.cancellation
        raise AgentRunCancelled(
            DEFAULT_CANCELLATION_MESSAGE,
            phase=exc.phase if exc is not None and exc.phase else "tool",
            node_name=(
                exc.node_name
                if exc is not None and exc.node_name
                else prepared.node_name
            ),
            source=exc.source if exc is not None else "tool_executor",
            details=error_details,
            state=state,
        ) from exc

    def _run_with_retry(
        self,
        tool_name: str,
        tool_input: BaseModel | dict[str, Any],
        context: ToolContext,
        *,
        step_id: str,
        preserve_success_after_cancel: bool = False,
        max_retries: int,
    ) -> tuple[ToolResult, int]:
        failed_attempts = 0
        retry_count = 0
        while True:
            raise_if_cancelled(
                self.cancel_token,
                phase="before_tool_attempt",
                source="tool_executor",
                details={"tool_name": tool_name, "step_id": step_id, "retry_count": retry_count},
            )
            result = self._run_once(tool_name, tool_input, context)
            if not (preserve_success_after_cancel and result.success):
                raise_if_cancelled(
                    self.cancel_token,
                    phase="after_tool_attempt",
                    source="tool_executor",
                    details={"tool_name": tool_name, "step_id": step_id, "retry_count": retry_count},
                )
            if result.success:
                return result, retry_count

            error_code = classify_error(result.error or "")
            failed_attempts += 1
            if (
                failed_attempts > max_retries
                or not self.execution_policy.retry.is_retryable(error_code)
            ):
                return result, retry_count

            retry_count += 1
            if self.execution_policy.retry.backoff_seconds > 0:
                _sleep_with_cancel(
                    self.cancel_token,
                    self.execution_policy.retry.backoff_seconds,
                    tool_name=tool_name,
                    step_id=step_id,
                    retry_count=retry_count,
                )

    def _run_once(
        self,
        tool_name: str,
        tool_input: BaseModel | dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        try:
            return self.registry.run(tool_name, tool_input, context)
        except AgentRunCancelled:
            raise
        except Exception as exc:  # pragma: no cover - registry boundary
            return ToolResult(tool_name=tool_name, success=False, error=sanitize_error_message(exc))

    def _emit(self, event: AgentEvent) -> None:
        if self.event_sink is not None:
            self.event_sink.emit(event)


def _sleep_with_cancel(
    cancel_token: Any | None,
    seconds: float,
    *,
    tool_name: str,
    step_id: str,
    retry_count: int,
) -> None:
    details = {"tool_name": tool_name, "step_id": step_id, "retry_count": retry_count}
    raise_if_cancelled(
        cancel_token,
        phase="before_tool_retry_sleep",
        source="tool_executor",
        details=details,
    )
    deadline = monotonic() + seconds
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            return
        sleep(min(remaining, 0.05))
        raise_if_cancelled(
            cancel_token,
            phase="after_tool_retry_sleep",
            source="tool_executor",
            details=details,
        )


def _append_tool_trace_event(
    trace_store: TraceStore | None,
    *,
    trace_id: str | None,
    state: AgentState,
    node_name: str,
    canonical_event: str,
    status: str,
    capability: str,
    tool_name: str,
    call_id: str,
    step_id: str,
    span_id: str,
    event_type: str = "observability",
    latency_ms: int | None = None,
    reported_latency_ms: int | None = None,
    retry_count: int | None = None,
    tool_contract: dict[str, Any] | None = None,
    input_summary: dict[str, Any] | None = None,
    output_summary: dict[str, Any] | None = None,
    provider: str | None = None,
    model: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    recovery_action: str | None = None,
) -> None:
    if trace_store is None or trace_id is None:
        return
    error = _tool_trace_error(
        error_code=error_code,
        error_message=error_message,
        recovery_action=recovery_action,
        step_id=step_id,
        retry_count=retry_count,
    )
    trace_store.append(
        TraceEvent(
            trace_id=trace_id,
            run_id=state.run_id,
            user_id=state.user_id,
            session_id=state.session_id,
            node_name=node_name,
            event_type=event_type,
            canonical_event=canonical_event,
            observation_type="span" if canonical_event != "tool.started" else None,
            observation_scope="iteration",
            span_id=span_id,
            capability=capability,
            tool_name=tool_name,
            provider=provider,
            model=model,
            status=status,
            latency_ms=latency_ms,
            error_code=error_code,
            input_summary=input_summary or {},
            output_summary=output_summary or {},
            attributes=_tool_trace_attributes(
                call_id=call_id,
                step_id=step_id,
                retry_count=retry_count,
                tool_contract=tool_contract,
                reported_latency_ms=reported_latency_ms,
            ),
            error=error,
        )
    )


def _tool_trace_attributes(
    *,
    call_id: str,
    step_id: str,
    retry_count: int | None,
    tool_contract: dict[str, Any] | None,
    reported_latency_ms: int | None = None,
) -> dict[str, Any]:
    attributes: dict[str, Any] = {
        "tool_call_id": call_id,
        "step_id": step_id,
    }
    if retry_count is not None:
        attributes["retry_count"] = retry_count
    if reported_latency_ms is not None:
        attributes["tool_reported_latency_ms"] = reported_latency_ms
    if tool_contract:
        attributes["tool_category"] = tool_contract.get("category")
        attributes["requires_confirmation"] = tool_contract.get(
            "requires_confirmation"
        )
        attributes["confirmation_pending"] = tool_contract.get(
            "confirmation_pending"
        )
    return {key: value for key, value in attributes.items() if value is not None}


def _tool_trace_error(
    *,
    error_code: str | None,
    error_message: str | None,
    recovery_action: str | None,
    step_id: str,
    retry_count: int | None,
) -> dict[str, Any] | None:
    if error_code is None and error_message is None and recovery_action is None:
        return None
    error = {
        "code": error_code,
        "message": sanitize_trace_value(error_message) if error_message else None,
        "recovery_action": recovery_action,
        "step_id": step_id,
        "retry_count": retry_count,
    }
    return {key: value for key, value in error.items() if value is not None}


def _preserve_success_after_cancel(tool_spec: ToolSpec) -> bool:
    return tool_spec.category != "read"


def _capability_name(tool_name: str, step: TaskStep | None) -> str:
    if step is not None:
        manifest_capability = canonical_capability_for_action(step.action)
        if manifest_capability is not None:
            return manifest_capability
    tool_map = {
        IMAGE_GENERATION_TOOL_NAME: IMAGE_GENERATION_CAPABILITY,
        MEMORY_RETRIEVAL_TOOL_NAME: MEMORY_RETRIEVAL_CAPABILITY,
        MEMORY_SAVE_TOOL_NAME: MEMORY_SAVE_CAPABILITY,
    }
    manifest_capability = canonical_capability_for_tool(tool_name)
    if manifest_capability is not None:
        return manifest_capability
    return tool_map.get(tool_name, tool_name)


def _cancelled_tool_result(
    tool_name: str,
    *,
    latency_ms: int,
    cancel_metadata: dict[str, Any] | None = None,
) -> ToolResult:
    metadata = build_realtime_turn_cancellation_metadata(
        cancel_metadata,
        phase="tool_running",
    )
    return ToolResult(
        tool_name=tool_name,
        success=False,
        error=f"{CANCELLATION_ERROR_CODE}: {DEFAULT_CANCELLATION_MESSAGE}",
        data={
            "cancelled": True,
            "stale_outputs": metadata["stale_outputs"],
            "can_reuse_tool_result": metadata["can_reuse_tool_result"],
            "speakable": metadata["speakable"],
            "realtime_turn_cancellation": metadata["realtime_turn_cancellation"],
        },
        latency_ms=latency_ms,
    )


def _bind_runtime_identity(tool: Any, tool_input: dict[str, Any], state: AgentState) -> dict[str, Any]:
    """Bind declared identity fields from authenticated runtime state."""

    fields = set(getattr(tool, "runtime_identity_fields", ()))
    if not fields:
        return tool_input
    bound = dict(tool_input)
    if "user_id" in fields:
        bound["user_id"] = state.user_id
    if "session_id" in fields:
        bound["session_id"] = state.session_id
    return bound


def _bind_runtime_media_inputs(tool: Any, tool_input: dict[str, Any], state: AgentState) -> dict[str, Any]:
    """Bind request-scoped media refs for tools without exposing them as model-visible facts."""

    if getattr(tool, "bind_request_video_ids", False) is not True:
        return tool_input
    if state.request.video_ids and is_trusted_agent_service_request(state.request):
        sanitized = dict(tool_input)
        sanitized.pop("video_ref", None)
        sanitized["video_ids"] = list(state.request.video_ids)
        return sanitized
    if tool_input.get("video_ref") or tool_input.get("video_ids"):
        return tool_input
    if not state.request.video_ids:
        return tool_input
    return {**tool_input, "video_ids": list(state.request.video_ids)}


def _bind_durable_idempotency(
    tool_input: dict[str, Any],
    *,
    step_id: str,
    context_metadata: dict[str, Any],
) -> dict[str, Any]:
    """Inject worker-issued idempotency keys from trusted runtime metadata."""

    raw_binding = context_metadata.get("durable_task_binding")
    if raw_binding is None:
        return tool_input
    binding = TrustedTaskBinding.model_validate(raw_binding)
    idempotency_key = binding.step_idempotency_keys.get(step_id)
    if not idempotency_key:
        return tool_input
    return {**tool_input, "idempotency_key": idempotency_key}


def _provider_name(payload: dict[str, Any]) -> str | None:
    value = payload.get("provider")
    return value if isinstance(value, str) and value else None


def _model_name(payload: dict[str, Any]) -> str | None:
    value = payload.get("model")
    return value if isinstance(value, str) and value else None


def _input_size_bytes(payload: dict[str, Any]) -> int | None:
    explicit = payload.get("input_size_bytes")
    if isinstance(explicit, int) and explicit >= 0:
        return explicit
    size = len(str(payload).encode("utf-8"))
    return size if size >= 0 else None


def _input_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "field_count": len(payload),
        "input_size_bytes": _input_size_bytes(payload),
        "media_count": sum(
            len(value)
            for key, value in payload.items()
            if key in {"image_ids", "video_ids", "reference_image_ids"} and isinstance(value, list)
        ),
        "prompt_length": len(payload.get("prompt", "") or payload.get("text", "") or payload.get("query", "")),
    }


def _output_summary(result: ToolResult) -> dict[str, Any]:
    data = result.data or {}
    payload: dict[str, Any] = {
        "success": result.success,
        "output_ref": result.output_ref,
        "error_code": classify_error(result.error or "") if result.error else None,
    }
    if isinstance(result.trace_summary, dict):
        safe_trace = sanitize_error_detail(result.trace_summary)
        if isinstance(safe_trace, dict):
            payload.update(
                {
                    key: value
                    for key, value in safe_trace.items()
                    if key not in {"raw_data_ref", "raw_provider_payload", "provider_raw_response"}
                }
            )
    else:
        payload["item_count"] = len(data.get("items", [])) if isinstance(data.get("items"), list) else None
    return payload


def _policy_safe_input_summary(
    payload: dict[str, Any],
) -> dict[str, Any]:
    if local_trace_content_enabled():
        safe_payload = sanitize_error_detail(payload)
        return safe_payload if isinstance(safe_payload, dict) else {}
    return {
        "redacted": True,
        "field_names": sorted(str(key) for key in payload),
        **_input_summary(payload),
    }


def _policy_safe_output_summary(
    result: ToolResult,
) -> dict[str, Any]:
    if local_trace_content_enabled():
        data = result.data if isinstance(result.data, dict) else {}
        safe_data = {
            key: value
            for key, value in data.items()
            if key not in {"raw_data_ref", "raw_provider_payload", "provider_raw_response"}
        }
        payload = sanitize_error_detail(
            {
                "success": result.success,
                "output_ref": result.output_ref,
                "raw_data_ref": result.raw_data_ref,
                "error": result.error,
                "data": safe_data,
                "model_observation": result.model_observation,
                "trace_summary": result.trace_summary,
                "audit_payload": _policy_safe_audit_payload(result),
            }
        )
        if isinstance(payload, dict):
            return {key: value for key, value in payload.items() if value is not None}
        return _output_summary(result)
    data = result.data if isinstance(result.data, dict) else {}
    trace = result.trace_summary if isinstance(result.trace_summary, dict) else {}
    approved_summary = trace.get("summary")
    return {
        "redacted": True,
        "success": result.success,
        "output_ref": result.output_ref,
        "raw_data_ref": sanitize_trace_value(result.raw_data_ref) if result.raw_data_ref else None,
        "error_code": classify_error(result.error or "") if result.error else None,
        "summary": (
            sanitize_error_message(approved_summary)[:240]
            if isinstance(approved_summary, str) and approved_summary.strip()
            else None
        ),
        "data_field_names": sorted(str(key) for key in data),
        "trace_field_names": sorted(str(key) for key in trace),
        "audit_payload": _policy_safe_audit_payload(result),
        "result_size_bytes": len(str(data).encode("utf-8")),
    }


def _policy_safe_audit_payload(
    result: ToolResult,
) -> dict[str, Any] | None:
    payload = result.audit_payload
    if not isinstance(payload, dict):
        return payload
    if local_trace_content_enabled():
        safe_payload = sanitize_error_detail(payload)
        return safe_payload if isinstance(safe_payload, dict) else None
    if payload.get("redacted") is True:
        safe_payload = sanitize_error_detail(payload)
        if isinstance(safe_payload, dict):
            return {
                key: value
                for key, value in safe_payload.items()
                if key
                in {
                    "provider",
                    "request_id",
                    "operation_id",
                    "status",
                    "item_count",
                    "redacted",
                }
            }
    return {
        "redacted": True,
        "field_names": sorted(str(key) for key in payload),
        "payload_size_bytes": len(str(payload).encode("utf-8")),
    }


def _policy_safe_error(error: str | None) -> str | None:
    if error is None:
        return error
    if local_trace_content_enabled():
        return sanitize_error_message(error)
    return classify_error(error)


def _effective_max_retries(
    *,
    tool_spec: ToolSpec,
    global_max_retries: int,
) -> int:
    return global_max_retries if tool_spec.category == "read" else 0


def _tool_confirmation_granted(metadata: dict[str, Any], tool_name: str) -> bool:
    confirmation = metadata.get("tool_confirmation")
    if not isinstance(confirmation, dict) or confirmation.get("confirmed") is not True:
        return False
    confirmed_tool = confirmation.get("tool_name")
    return confirmed_tool == tool_name


def _confirmation_required_result(*, tool_name: str, latency_ms: int) -> ToolResult:
    return ToolResult(
        tool_name=tool_name,
        success=True,
        data={
            "status": "confirmation_required",
            "summary": "Tool execution requires user confirmation before continuing.",
            "requires_confirmation": True,
        },
        output_ref=f"local://tool-confirmations/{tool_name}",
        latency_ms=latency_ms,
    )


def _execution_summary(
    tool_spec: ToolSpec,
    disposition: str,
) -> dict[str, Any]:
    return {
        "category": tool_spec.category,
        "requires_confirmation": disposition == "confirmation",
        "confirmation_configured": tool_spec.requires_confirmation,
        "confirmation_pending": disposition == "confirmation",
    }
