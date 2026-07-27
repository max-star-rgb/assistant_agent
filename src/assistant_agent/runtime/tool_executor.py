"""Serial governed Tool execution used by workflows and assistant loops."""

from time import monotonic, perf_counter, sleep
from typing import Any

from pydantic import BaseModel

from assistant_agent.runtime.cancellation import (
    AgentRunCancelled,
    CANCELLATION_ERROR_CODE,
    DEFAULT_CANCELLATION_MESSAGE,
    raise_if_cancelled,
)
from assistant_agent.runtime.legacy_tool_mapping import (
    canonical_capability_for_action,
    canonical_capability_for_tool,
)
from assistant_agent.runtime.recovery import RecoveryPolicy, ToolFailureMode, classify_error
from assistant_agent.runtime.state import AgentState
from assistant_agent.api.models import api_error
from assistant_agent.tools.capability_output import contract_summary
from assistant_agent.runtime.events import AgentEvent
from assistant_agent.identity import RequestIdentity
from assistant_agent.runtime.planning_models import TaskStep
from assistant_agent.gateway.cancellation_models import (
    build_realtime_turn_cancellation_metadata,
)
from assistant_agent.tools.ids import (
    IMAGE_GENERATION_CAPABILITY,
    IMAGE_GENERATION_TOOL_NAME,
)
from assistant_agent.tools.models import ToolResult, ToolSpec
from assistant_agent.runtime.event_sink import EventSink
from assistant_agent.providers.provider_errors import (
    sanitize_error_detail,
    sanitize_error_message,
)
from assistant_agent.providers.provider_policy import ProviderExecutionPolicy
from assistant_agent.tools.tool_call_boundary import (
    build_post_tool_call_summary,
    build_pre_tool_call_summary,
)
from assistant_agent.observability.trace_content_policy import local_trace_content_enabled
from assistant_agent.observability.trace_store import (
    TraceEvent,
    TraceStore,
    new_span_id,
    sanitize_trace_value,
)
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.input_binding import bind_runtime_tool_input
from assistant_agent.tools.plugins.registry_factory import create_default_registry
from assistant_agent.tools.registry import ToolRegistry


class ToolExecutor:
    """Execute one Tool serially and commit its lifecycle to AgentState."""

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
        runtime_input: dict[str, Any] | None = None,
        failure_mode: ToolFailureMode = "stop_run",
    ) -> ToolResult:
        """Bind, confirm, invoke and commit one governed Tool call."""

        effective_node_name = node_name or "tool_executor"
        raise_if_cancelled(
            self.cancel_token,
            phase="before_tool",
            node_name=effective_node_name,
            source="tool_executor",
            details={"tool_name": tool_name, "step_id": step_id},
            state=state,
        )
        tool = self.registry.get(tool_name)
        if validated_input is not None and not isinstance(
            validated_input, tool.input_schema
        ):
            raise TypeError(f"validated_input does not match {tool_name} input_schema")
        normalized_input = (
            validated_input.model_dump(mode="python")
            if validated_input is not None
            else tool_input
        )
        bound_input = bind_runtime_tool_input(
            tool,
            normalized_input,
            state=state,
            step_id=step_id,
            context_metadata=self.context_metadata,
            runtime_input=runtime_input,
        )
        tool_spec = self.registry.get_spec(tool_name)
        execution_summary = {
            "category": tool_spec.category,
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
        tool_span_id = new_span_id()
        _append_tool_trace_event(
            trace_store,
            trace_id=trace_id,
            state=state,
            node_name=effective_node_name,
            canonical_event="tool.started",
            status="started",
            capability=capability,
            tool_name=tool_name,
            tool_call_id=call.tool_call_id,
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
                    "tool_call_id": call.tool_call_id,
                    "step_id": step_id,
                    "pre_tool_call": pre_tool_call,
                },
            )
        )

        retry_count = 0
        before_tool_execution = self.context_metadata.get("_before_tool_execution")
        if callable(before_tool_execution):
            before_tool_execution()
        context = ToolContext(
            run_id=state.run_id,
            user_id=state.user_id,
            session_id=state.session_id,
            metadata={
                **{
                    key: value
                    for key, value in self.context_metadata.items()
                    if key != "_before_tool_execution"
                },
                "request_text": state.request.text or "",
                "request_metadata": dict(state.request.metadata),
                "request_identity": RequestIdentity.from_user_request(
                    state.request,
                    agent_id=state.agent_id,
                ).model_dump(mode="json"),
                "run_tool_catalog": (
                    state.run_tool_catalog.model_dump(mode="json")
                    if state.run_tool_catalog is not None
                    else None
                ),
            },
            cancel_token=self.cancel_token,
        )
        invocation_input: BaseModel | dict[str, Any] = bound_input
        if validated_input is not None and bound_input == normalized_input:
            invocation_input = validated_input
        elif validated_input is not None:
            invocation_input = tool.input_schema.model_validate(bound_input)
        try:
            result, retry_count = self._run_with_retry(
                tool_name,
                invocation_input,
                context,
                step_id=step_id,
                preserve_success_after_cancel=tool_spec.category != "read",
                max_retries=_effective_max_retries(
                    tool_spec=tool_spec,
                    global_max_retries=self.execution_policy.retry.max_retries,
                ),
            )
        except AgentRunCancelled as exc:
            self._commit_cancellation(
                state=state,
                exc=exc,
                tool_name=tool_name,
                step_id=step_id,
                tool_call_id=call.tool_call_id,
                capability=capability,
                started_at=started_at,
                tool_span_id=tool_span_id,
                tool_input=bound_input,
                tool_spec=tool_spec,
                trace_store=trace_store,
                trace_id=trace_id,
                node_name=effective_node_name,
            )
            raise AssertionError("cancellation commit must raise")

        reported_latency_ms = result.latency_ms
        latency_ms = int((perf_counter() - started_at) * 1000)
        if result.latency_ms is None:
            result.latency_ms = latency_ms
        tool_contract = _execution_summary(tool_spec)

        error_code = None
        recovery_action = None
        retryable = False
        if result.success:
            state.complete_tool_call(call.tool_call_id, result)
            event_type = "tool_finished"
            canonical_event = "tool.finished"
            status = "succeeded"
        else:
            decision = self.recovery_policy.decide(
                result,
                step,
                failure_mode=failure_mode,
            )
            result.error = decision.message
            error_code = decision.error_code
            recovery_action = decision.action
            retryable = decision.retryable
            state.fail_tool_call(
                call.tool_call_id,
                decision.message,
                result,
                error_details={
                    "code": decision.error_code,
                    "recovery_action": decision.action,
                    "optional_step": decision.optional_step,
                    "retryable": decision.retryable,
                    "step_id": step_id,
                    "retry_count": retry_count,
                },
                stop_run=decision.action == "stop_with_error",
            )
            event_type = "tool_failed"
            canonical_event = "tool.failed"
            status = "failed"

        post_tool_call = build_post_tool_call_summary(
            tool_name=tool_name,
            result=result,
            state=state,
            step_id=step_id,
            tool_call_id=call.tool_call_id,
            latency_ms=latency_ms,
            retry_count=retry_count,
            tool_contract=tool_contract,
            registry=self.registry,
        )
        payload = {
            "tool_call_id": call.tool_call_id,
            "step_id": step_id,
            "latency_ms": latency_ms,
            "retry_count": retry_count,
            "post_tool_call": post_tool_call,
        }
        payload["contract"] = contract_summary(result.contract)
        if error_code is not None:
            payload.update(
                {
                    "code": error_code,
                    "recovery_action": recovery_action,
                }
            )
        self._emit(
            AgentEvent(
                type=event_type,
                session_id=state.session_id,
                run_id=state.run_id,
                tool_name=tool_name,
                output_ref=result.output_ref if result.success else None,
                error=(
                    api_error(
                        error_code or "tool_failed",
                        result.error or "Tool failed.",
                        detail={
                            "step_id": step_id,
                            "recovery_action": recovery_action,
                        },
                        recoverable=retryable,
                    ).model_dump(mode="json")
                    if not result.success
                    else None
                ),
                payload=payload,
            )
        )
        _append_tool_trace_event(
            trace_store,
            trace_id=trace_id,
            state=state,
            node_name=effective_node_name,
            event_type="observability" if result.success else "tool_failed",
            canonical_event=canonical_event,
            status=str(post_tool_call.get("status") or status),
            capability=capability,
            tool_name=tool_name,
            tool_call_id=call.tool_call_id,
            step_id=step_id,
            span_id=tool_span_id,
            latency_ms=latency_ms,
            reported_latency_ms=reported_latency_ms,
            retry_count=retry_count,
            tool_contract=tool_contract,
            input_summary=_policy_safe_input_summary(bound_input),
            output_summary=_policy_safe_output_summary(result),
            provider=_provider_name(result.data or {}),
            model=_model_name(result.data or {}),
            error_code=error_code,
            error_message=_policy_safe_error(result.error) if not result.success else None,
            recovery_action=recovery_action,
        )
        return result

    def _commit_cancellation(
        self,
        *,
        state: AgentState,
        exc: AgentRunCancelled,
        tool_name: str,
        step_id: str,
        tool_call_id: str,
        capability: str,
        started_at: float,
        tool_span_id: str,
        tool_input: dict[str, Any],
        tool_spec: ToolSpec,
        trace_store: TraceStore | None,
        trace_id: str | None,
        node_name: str,
    ) -> None:
        latency_ms = int((perf_counter() - started_at) * 1000)
        error_details = build_realtime_turn_cancellation_metadata(
            {
                **exc.details,
                "step_id": step_id,
                "tool_name": tool_name,
                "retryable": False,
            },
            phase="tool_running",
        )
        result = _cancelled_tool_result(
            tool_name,
            latency_ms=latency_ms,
            cancel_metadata=error_details,
        )
        tool_contract = _execution_summary(tool_spec)
        post_tool_call = build_post_tool_call_summary(
            tool_name=tool_name,
            result=result,
            state=state,
            step_id=step_id,
            tool_call_id=tool_call_id,
            latency_ms=latency_ms,
            retry_count=0,
            cancel_metadata=error_details,
            tool_contract=tool_contract,
            registry=self.registry,
        )
        state.fail_tool_call(
            tool_call_id,
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
                tool_name=tool_name,
                error=api_error(
                    CANCELLATION_ERROR_CODE,
                    DEFAULT_CANCELLATION_MESSAGE,
                    detail={"step_id": step_id, "source": tool_name},
                    recoverable=False,
                ).model_dump(mode="json"),
                payload={
                    "tool_call_id": tool_call_id,
                    "step_id": step_id,
                    "latency_ms": latency_ms,
                    "retry_count": 0,
                    "code": CANCELLATION_ERROR_CODE,
                    "post_tool_call": post_tool_call,
                },
            )
        )
        _append_tool_trace_event(
            trace_store,
            trace_id=trace_id,
            state=state,
            node_name=node_name,
            event_type="tool_failed",
            canonical_event="tool.failed",
            status="failed",
            capability=capability,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            step_id=step_id,
            span_id=tool_span_id,
            latency_ms=latency_ms,
            reported_latency_ms=result.latency_ms,
            retry_count=0,
            tool_contract=tool_contract,
            input_summary=_policy_safe_input_summary(tool_input),
            output_summary={"cancelled": True},
            error_code=CANCELLATION_ERROR_CODE,
            error_message=DEFAULT_CANCELLATION_MESSAGE,
            recovery_action="cancelled",
        )
        raise AgentRunCancelled(
            DEFAULT_CANCELLATION_MESSAGE,
            phase=exc.phase or "tool",
            node_name=exc.node_name or node_name,
            source=exc.source,
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
        preserve_success_after_cancel: bool,
        max_retries: int,
    ) -> tuple[ToolResult, int]:
        failed_attempts = 0
        retry_count = 0
        while True:
            raise_if_cancelled(
                self.cancel_token,
                phase="before_tool_attempt",
                source="tool_executor",
                details={
                    "tool_name": tool_name,
                    "step_id": step_id,
                    "retry_count": retry_count,
                },
            )
            result = self._run_once(tool_name, tool_input, context)
            if not (preserve_success_after_cancel and result.success):
                raise_if_cancelled(
                    self.cancel_token,
                    phase="after_tool_attempt",
                    source="tool_executor",
                    details={
                        "tool_name": tool_name,
                        "step_id": step_id,
                        "retry_count": retry_count,
                    },
                )
            if result.success:
                return result, retry_count
            failed_attempts += 1
            if (
                failed_attempts > max_retries
                or not self.execution_policy.retry.is_retryable(
                    classify_error(result.error or "")
                )
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
            return ToolResult(
                tool_name=tool_name,
                success=False,
                error=sanitize_error_message(exc),
            )

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
    details = {
        "tool_name": tool_name,
        "step_id": step_id,
        "retry_count": retry_count,
    }
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
    tool_call_id: str,
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
    error = None
    if error_code or error_message or recovery_action:
        error = {
            key: value
            for key, value in {
                "code": error_code,
                "message": (
                    sanitize_trace_value(error_message) if error_message else None
                ),
                "recovery_action": recovery_action,
                "step_id": step_id,
                "retry_count": retry_count,
            }.items()
            if value is not None
        }
    attributes = {
        "tool_call_id": tool_call_id,
        "step_id": step_id,
        "retry_count": retry_count,
        "tool_reported_latency_ms": reported_latency_ms,
        "tool_category": (tool_contract or {}).get("category"),
    }
    trace_store.append(
        TraceEvent(
            trace_id=trace_id,
            run_id=state.run_id,
            user_id=state.user_id,
            session_id=state.session_id,
            node_name=node_name,
            event_type=event_type,
            canonical_event=canonical_event,
            observation_type=(
                "span" if canonical_event != "tool.started" else None
            ),
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
            attributes={
                key: value for key, value in attributes.items() if value is not None
            },
            error=error,
        )
    )


def _capability_name(tool_name: str, step: TaskStep | None) -> str:
    if step is not None:
        capability = canonical_capability_for_action(step.action)
        if capability is not None:
            return capability
    capability = canonical_capability_for_tool(tool_name)
    if capability is not None:
        return capability
    return {
        IMAGE_GENERATION_TOOL_NAME: IMAGE_GENERATION_CAPABILITY
    }.get(tool_name, tool_name)


def _cancelled_tool_result(
    tool_name: str,
    *,
    latency_ms: int,
    cancel_metadata: dict[str, Any],
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


def _policy_safe_input_summary(payload: dict[str, Any]) -> dict[str, Any]:
    if local_trace_content_enabled():
        safe = sanitize_error_detail(payload)
        return safe if isinstance(safe, dict) else {}
    return {
        "redacted": True,
        "field_names": sorted(str(key) for key in payload),
        "input_size_bytes": len(str(payload).encode("utf-8")),
    }


def _policy_safe_output_summary(result: ToolResult) -> dict[str, Any]:
    if local_trace_content_enabled():
        data = result.data if isinstance(result.data, dict) else {}
        safe = sanitize_error_detail(
            {
                "success": result.success,
                "output_ref": result.output_ref,
                "raw_data_ref": result.raw_data_ref,
                "error": result.error,
                "data": {
                    key: value
                    for key, value in data.items()
                    if key
                    not in {
                        "raw_data_ref",
                        "raw_provider_payload",
                        "provider_raw_response",
                    }
                },
                "model_observation": result.model_observation,
                "trace_summary": result.trace_summary,
                "audit_payload": result.audit_payload,
            }
        )
        return (
            {key: value for key, value in safe.items() if value is not None}
            if isinstance(safe, dict)
            else {}
        )
    data = result.data if isinstance(result.data, dict) else {}
    return {
        "redacted": True,
        "success": result.success,
        "output_ref": result.output_ref,
        "raw_data_ref": (
            sanitize_trace_value(result.raw_data_ref)
            if result.raw_data_ref
            else None
        ),
        "error_code": classify_error(result.error or "") if result.error else None,
        "data_field_names": sorted(str(key) for key in data),
        "result_size_bytes": len(str(data).encode("utf-8")),
    }


def _provider_name(payload: dict[str, Any]) -> str | None:
    value = payload.get("provider")
    return value if isinstance(value, str) and value else None


def _model_name(payload: dict[str, Any]) -> str | None:
    value = payload.get("model")
    return value if isinstance(value, str) and value else None


def _policy_safe_error(error: str | None) -> str | None:
    if error is None:
        return None
    if local_trace_content_enabled():
        return sanitize_error_message(error)
    return classify_error(error)


def _effective_max_retries(
    *,
    tool_spec: ToolSpec,
    global_max_retries: int,
) -> int:
    return global_max_retries if tool_spec.category == "read" else 0


def _execution_summary(tool_spec: ToolSpec) -> dict[str, Any]:
    return {
        "category": tool_spec.category,
    }
