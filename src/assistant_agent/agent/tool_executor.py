"""Tool execution service used by workflows and LangGraph nodes."""

from time import monotonic, perf_counter, sleep
from typing import Any

from assistant_agent.agent.cancellation import (
    AgentRunCancelled,
    CANCELLATION_ERROR_CODE,
    DEFAULT_CANCELLATION_MESSAGE,
    raise_if_cancelled,
)
from assistant_agent.agent.state import AgentState
from assistant_agent.agent.recovery import RecoveryPolicy, classify_error
from assistant_agent.schemas.api import api_error
from assistant_agent.schemas.capability_output import build_capability_output_contract, contract_summary
from assistant_agent.schemas.events import AgentEvent
from assistant_agent.schemas.planning import TaskStep
from assistant_agent.schemas.tools import ToolResult, ToolSideEffectPolicy
from assistant_agent.services.event_sink import EventSink
from assistant_agent.services.provider_budget import ProviderCallBudget
from assistant_agent.services.provider_errors import sanitize_error_detail, sanitize_error_message
from assistant_agent.services.provider_policy import ProviderExecutionPolicy
from assistant_agent.services.tool_call_boundary import (
    build_post_tool_call_summary,
    build_pre_tool_call_summary,
)
from assistant_agent.services.tool_history import ToolHistoryStore
from assistant_agent.services.tool_policy import ToolPolicyInterpreter
from assistant_agent.services.tool_risk_gate import (
    ToolIdempotencyLedger,
    confirmation_required_result,
    duplicate_suppressed_result,
    evaluate_tool_risk,
    get_default_tool_idempotency_ledger,
    record_successful_idempotent_result,
)
from assistant_agent.services.trace_store import TraceEvent, TraceStore, sanitize_trace_value
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.registry import ToolRegistry, create_default_registry


class ToolExecutor:
    """Run tools through the registry and update AgentState records."""

    def __init__(
        self,
        registry: ToolRegistry | None = None,
        tool_history: ToolHistoryStore | None = None,
        event_sink: EventSink | None = None,
        recovery_policy: RecoveryPolicy | None = None,
        execution_policy: ProviderExecutionPolicy | None = None,
        context_metadata: dict[str, Any] | None = None,
        cancel_token: Any | None = None,
        idempotency_ledger: ToolIdempotencyLedger | None = None,
    ) -> None:
        self.registry = registry or create_default_registry()
        self.tool_history = tool_history
        self.event_sink = event_sink
        self.recovery_policy = recovery_policy or RecoveryPolicy()
        self.execution_policy = execution_policy or ProviderExecutionPolicy.from_env()
        self.context_metadata = dict(context_metadata or {})
        self.cancel_token = cancel_token
        self.idempotency_ledger = idempotency_ledger or get_default_tool_idempotency_ledger()

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
    ) -> ToolResult:
        raise_if_cancelled(
            self.cancel_token,
            phase="before_tool",
            node_name=node_name or "tool_executor",
            source="tool_executor",
            details={"tool_name": tool_name, "step_id": step_id},
            state=state,
        )
        tool_input = _bind_runtime_identity(tool_name, tool_input, state)
        risk_decision = evaluate_tool_risk(
            tool_name=tool_name,
            tool_input=tool_input,
            request=state.request,
            state=state,
            step_id=step_id,
            policy=_side_effect_policy_for_registered_tool(self.registry, tool_name),
            idempotency_required=_idempotency_required_for_registered_tool(self.registry, tool_name),
        )
        pre_tool_call = build_pre_tool_call_summary(
            tool_name=tool_name,
            tool_input=tool_input,
            registry=self.registry,
            request=state.request,
            state=state,
            step_id=step_id,
            cancel_token=self.cancel_token,
            risk_gate=risk_decision.risk_summary(),
            idempotency=risk_decision.idempotency_summary(),
        )
        call = state.add_tool_call(tool_name, tool_input)
        capability = _capability_name(tool_name, step)
        budget = state.provider_budget
        started_at = perf_counter()
        tool_span_id = f"span_{call.call_id}"
        _append_tool_trace_event(
            trace_store,
            trace_id=trace_id,
            state=state,
            node_name=node_name or "tool_executor",
            canonical_event="tool.started",
            status="started",
            capability=capability,
            tool_name=tool_name,
            call_id=call.call_id,
            step_id=step_id,
            span_id=tool_span_id,
            risk_gate=risk_decision.risk_summary(),
            idempotency=risk_decision.idempotency_summary(),
            input_summary=_input_summary(tool_input),
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
        if self.tool_history is not None:
            self.tool_history.record_start(
                state.run_id,
                call.call_id,
                tool_name,
                tool_input,
                user_id=state.user_id,
                session_id=state.session_id,
            )
        idempotency_record = None
        if risk_decision.idempotency_required and risk_decision.idempotency_key is not None:
            idempotency_record = self.idempotency_ledger.get(
                user_id=state.user_id,
                session_id=state.session_id,
                tool_name=tool_name,
                idempotency_key=risk_decision.idempotency_key,
            )
        if idempotency_record is not None:
            latency_ms = int((perf_counter() - started_at) * 1000)
            result = duplicate_suppressed_result(
                tool_name=tool_name,
                record=idempotency_record,
                decision=risk_decision,
                latency_ms=latency_ms,
            )
            state.complete_tool_call(call.call_id, result)
            post_tool_call = build_post_tool_call_summary(
                tool_name=tool_name,
                result=result,
                state=state,
                step_id=step_id,
                call_id=call.call_id,
                latency_ms=latency_ms,
                retry_count=0,
                risk_gate=risk_decision.risk_summary(),
                idempotency=risk_decision.idempotency_summary(duplicate_suppressed=True),
                registry=self.registry,
            )
            self._emit(
                AgentEvent(
                    type="tool_finished",
                    session_id=state.session_id,
                    run_id=state.run_id,
                    tool_name=tool_name,
                    output_ref=result.output_ref,
                    payload={
                        "call_id": call.call_id,
                        "step_id": step_id,
                        "latency_ms": latency_ms,
                        "retry_count": 0,
                        "post_tool_call": post_tool_call,
                    },
                )
            )
            if self.tool_history is not None:
                self.tool_history.record_end(
                    state.run_id,
                    call.call_id,
                    tool_name,
                    "succeeded",
                    latency_ms,
                    output_ref=result.output_ref,
                    user_id=state.user_id,
                    session_id=state.session_id,
                    output_summary=_output_summary(result),
                    audit_payload=result.audit_payload,
                    raw_data_ref=result.raw_data_ref,
                )
            _append_tool_trace_event(
                trace_store,
                trace_id=trace_id,
                state=state,
                node_name=node_name or "tool_executor",
                canonical_event="tool.finished",
                status=str(post_tool_call.get("status") or "succeeded"),
                capability=capability,
                tool_name=tool_name,
                call_id=call.call_id,
                step_id=step_id,
                span_id=tool_span_id,
                latency_ms=latency_ms,
                retry_count=0,
                risk_gate=risk_decision.risk_summary(),
                idempotency=risk_decision.idempotency_summary(duplicate_suppressed=True),
                input_summary=_input_summary(tool_input),
                output_summary=_output_summary(result),
                provider=_provider_name(result.data or {}),
                model=_model_name(result.data or {}),
            )
            return result

        if not risk_decision.allow_execute:
            latency_ms = int((perf_counter() - started_at) * 1000)
            result = confirmation_required_result(
                tool_name=tool_name,
                decision=risk_decision,
                latency_ms=latency_ms,
            )
            state.complete_tool_call(call.call_id, result)
            post_tool_call = build_post_tool_call_summary(
                tool_name=tool_name,
                result=result,
                state=state,
                step_id=step_id,
                call_id=call.call_id,
                latency_ms=latency_ms,
                retry_count=0,
                risk_gate=risk_decision.risk_summary(),
                idempotency=risk_decision.idempotency_summary(),
                registry=self.registry,
            )
            self._emit(
                AgentEvent(
                    type="tool_finished",
                    session_id=state.session_id,
                    run_id=state.run_id,
                    tool_name=tool_name,
                    output_ref=result.output_ref,
                    payload={
                        "call_id": call.call_id,
                        "step_id": step_id,
                        "latency_ms": latency_ms,
                        "retry_count": 0,
                        "post_tool_call": post_tool_call,
                    },
                )
            )
            if self.tool_history is not None:
                self.tool_history.record_end(
                    state.run_id,
                    call.call_id,
                    tool_name,
                    "succeeded",
                    latency_ms,
                    output_ref=result.output_ref,
                    user_id=state.user_id,
                    session_id=state.session_id,
                    output_summary=_output_summary(result),
                    audit_payload=result.audit_payload,
                    raw_data_ref=result.raw_data_ref,
                )
            _append_tool_trace_event(
                trace_store,
                trace_id=trace_id,
                state=state,
                node_name=node_name or "tool_executor",
                canonical_event="tool.finished",
                status=str(post_tool_call.get("status") or "pending_confirmation"),
                capability=capability,
                tool_name=tool_name,
                call_id=call.call_id,
                step_id=step_id,
                span_id=tool_span_id,
                latency_ms=latency_ms,
                retry_count=0,
                risk_gate=risk_decision.risk_summary(),
                idempotency=risk_decision.idempotency_summary(),
                input_summary=_input_summary(tool_input),
                output_summary=_output_summary(result),
                provider=_provider_name(result.data or {}),
                model=_model_name(result.data or {}),
            )
            return result

        budget_error = budget.check_before_call(
            capability=capability,
            provider=_provider_name(tool_input),
            estimated_cost=_estimated_cost(tool_input),
            input_size_bytes=_input_size_bytes(tool_input),
        )
        if budget_error is not None:
            latency_ms = int((perf_counter() - started_at) * 1000)
            optional_step = bool(step.optional) if step is not None else False
            recovery_action = "continue_with_partial_result" if optional_step else "stop_with_error"
            contract = build_capability_output_contract(
                capability=capability,
                status="failed",
                errors=[budget_error.model_dump(mode="json")],
                metadata={"provider_budget": budget.summary()},
            )
            result = ToolResult(
                tool_name=tool_name,
                success=False,
                error=f"{budget_error.code}: {budget_error.message}",
                data={"provider_budget": budget.summary()},
                latency_ms=latency_ms,
                contract=contract,
            )
            post_tool_call = build_post_tool_call_summary(
                tool_name=tool_name,
                result=result,
                state=state,
                step_id=step_id,
                call_id=call.call_id,
                latency_ms=latency_ms,
                retry_count=0,
                risk_gate=risk_decision.risk_summary(),
                idempotency=risk_decision.idempotency_summary(),
                registry=self.registry,
            )
            state.fail_tool_call(
                call.call_id,
                budget_error.message,
                result,
                error_details={
                    "code": budget_error.code,
                    "recovery_action": recovery_action,
                    "optional_step": optional_step,
                    "retryable": False,
                    "step_id": step_id,
                    "provider_budget": budget.summary(),
                },
                stop_run=not optional_step,
            )
            self._emit(
                AgentEvent(
                    type="tool_failed",
                    session_id=state.session_id,
                    run_id=state.run_id,
                    tool_name=tool_name,
                    error=api_error(
                        budget_error.code,
                        budget_error.message,
                        detail={"step_id": step_id, "recovery_action": recovery_action},
                        recoverable=False,
                    ).model_dump(mode="json"),
                    payload={
                        "call_id": call.call_id,
                        "step_id": step_id,
                        "latency_ms": latency_ms,
                        "retry_count": 0,
                        "code": budget_error.code,
                        "recovery_action": recovery_action,
                        "contract": contract_summary(result.contract),
                        "post_tool_call": post_tool_call,
                    },
                )
            )
            if self.tool_history is not None:
                self.tool_history.record_end(
                    state.run_id,
                    call.call_id,
                    tool_name,
                    "failed",
                    latency_ms,
                    error=budget_error.message,
                    user_id=state.user_id,
                    session_id=state.session_id,
                    output_summary=_output_summary(result),
                    audit_payload=result.audit_payload,
                    raw_data_ref=result.raw_data_ref,
                )
            _append_tool_trace_event(
                trace_store,
                trace_id=trace_id,
                state=state,
                node_name=node_name or "tool_executor",
                event_type="tool_failed",
                canonical_event="tool.failed",
                status="failed",
                capability=capability,
                tool_name=tool_name,
                call_id=call.call_id,
                step_id=step_id,
                span_id=tool_span_id,
                latency_ms=latency_ms,
                retry_count=0,
                risk_gate=risk_decision.risk_summary(),
                idempotency=risk_decision.idempotency_summary(),
                input_summary=_input_summary(tool_input),
                output_summary={"provider_budget": budget.summary()},
                error_code=budget_error.code,
                error_message=budget_error.message,
                recovery_action=recovery_action,
            )
            return result

        try:
            result, retry_count = self._run_with_retry(
                tool_name,
                tool_input,
                ToolContext(
                    run_id=state.run_id,
                    user_id=state.user_id,
                    session_id=state.session_id,
                    metadata={
                        **self.context_metadata,
                        "request_text": state.request.text or "",
                        "request_metadata": dict(state.request.metadata),
                    },
                    cancel_token=self.cancel_token,
                ),
                step_id=step_id,
                preserve_success_after_cancel=_preserve_success_after_cancel(risk_decision),
            )
        except AgentRunCancelled as exc:
            latency_ms = int((perf_counter() - started_at) * 1000)
            result = _cancelled_tool_result(tool_name, latency_ms=latency_ms)
            error_details = {
                **exc.details,
                "step_id": step_id,
                "tool_name": tool_name,
                "retryable": False,
            }
            post_tool_call = build_post_tool_call_summary(
                tool_name=tool_name,
                result=result,
                state=state,
                step_id=step_id,
                call_id=call.call_id,
                latency_ms=latency_ms,
                retry_count=0,
                cancel_metadata=error_details,
                risk_gate=risk_decision.risk_summary(),
                idempotency=risk_decision.idempotency_summary(),
                registry=self.registry,
            )
            state.fail_tool_call(
                call.call_id,
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
                        "call_id": call.call_id,
                        "step_id": step_id,
                        "latency_ms": latency_ms,
                        "retry_count": 0,
                        "code": CANCELLATION_ERROR_CODE,
                        "post_tool_call": post_tool_call,
                    },
                )
            )
            if self.tool_history is not None:
                self.tool_history.record_end(
                    state.run_id,
                    call.call_id,
                    tool_name,
                    "failed",
                    latency_ms,
                    error=DEFAULT_CANCELLATION_MESSAGE,
                    user_id=state.user_id,
                    session_id=state.session_id,
                    output_summary=_output_summary(result),
                    audit_payload=result.audit_payload,
                    raw_data_ref=result.raw_data_ref,
                )
            _append_tool_trace_event(
                trace_store,
                trace_id=trace_id,
                state=state,
                node_name=node_name or "tool_executor",
                event_type="tool_failed",
                canonical_event="tool.failed",
                status="failed",
                capability=capability,
                tool_name=tool_name,
                call_id=call.call_id,
                step_id=step_id,
                span_id=tool_span_id,
                latency_ms=latency_ms,
                retry_count=0,
                risk_gate=risk_decision.risk_summary(),
                idempotency=risk_decision.idempotency_summary(),
                input_summary=_input_summary(tool_input),
                output_summary={"cancelled": True},
                error_code=CANCELLATION_ERROR_CODE,
                error_message=DEFAULT_CANCELLATION_MESSAGE,
                recovery_action="cancelled",
            )
            raise AgentRunCancelled(
                DEFAULT_CANCELLATION_MESSAGE,
                phase=exc.phase or "tool",
                node_name=exc.node_name or node_name or "tool_executor",
                source=exc.source,
                details=error_details,
                state=state,
            ) from exc
        latency_ms = int((perf_counter() - started_at) * 1000)
        if result.latency_ms is None:
            result.latency_ms = latency_ms
        _record_provider_budget_call(
            budget,
            run_id=state.run_id,
            capability=capability,
            tool_input=tool_input,
            result=result,
            latency_ms=result.latency_ms or latency_ms,
        )

        if result.success:
            idempotency_record = record_successful_idempotent_result(
                ledger=self.idempotency_ledger,
                decision=risk_decision,
                state=state,
                result=result,
            )
            state.complete_tool_call(call.call_id, result)
            post_tool_call = build_post_tool_call_summary(
                tool_name=tool_name,
                result=result,
                state=state,
                step_id=step_id,
                call_id=call.call_id,
                latency_ms=result.latency_ms or latency_ms,
                retry_count=retry_count,
                risk_gate=risk_decision.risk_summary(),
                idempotency={
                    **risk_decision.idempotency_summary(),
                    "status": idempotency_record.status if idempotency_record is not None else None,
                },
                registry=self.registry,
            )
            self._emit(
                AgentEvent(
                    type="tool_finished",
                    session_id=state.session_id,
                    run_id=state.run_id,
                    tool_name=tool_name,
                    output_ref=result.output_ref,
                    payload={
                        "call_id": call.call_id,
                        "step_id": step_id,
                        "latency_ms": result.latency_ms or latency_ms,
                        "retry_count": retry_count,
                        "contract": contract_summary(result.contract),
                        "post_tool_call": post_tool_call,
                    },
                )
            )
            if self.tool_history is not None:
                self.tool_history.record_end(
                    state.run_id,
                    call.call_id,
                    tool_name,
                    "succeeded",
                    result.latency_ms or latency_ms,
                    output_ref=result.output_ref,
                    user_id=state.user_id,
                    session_id=state.session_id,
                    output_summary=_output_summary(result),
                    audit_payload=result.audit_payload,
                    raw_data_ref=result.raw_data_ref,
                )
            _append_tool_trace_event(
                trace_store,
                trace_id=trace_id,
                state=state,
                node_name=node_name or "tool_executor",
                canonical_event="tool.finished",
                status=str(post_tool_call.get("status") or "succeeded"),
                capability=capability,
                tool_name=tool_name,
                call_id=call.call_id,
                step_id=step_id,
                span_id=tool_span_id,
                latency_ms=result.latency_ms or latency_ms,
                retry_count=retry_count,
                risk_gate=risk_decision.risk_summary(),
                idempotency={
                    **risk_decision.idempotency_summary(),
                    "status": idempotency_record.status if idempotency_record is not None else None,
                },
                input_summary=_input_summary(tool_input),
                output_summary=_output_summary(result),
                provider=_provider_name(result.data or {}),
                model=_model_name(result.data or {}),
            )
        else:
            decision = self.recovery_policy.decide(result, step)
            result.error = decision.message
            post_tool_call = build_post_tool_call_summary(
                tool_name=tool_name,
                result=result,
                state=state,
                step_id=step_id,
                call_id=call.call_id,
                latency_ms=result.latency_ms or latency_ms,
                retry_count=retry_count,
                risk_gate=risk_decision.risk_summary(),
                idempotency=risk_decision.idempotency_summary(),
                registry=self.registry,
            )
            state.fail_tool_call(
                call.call_id,
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
            self._emit(
                AgentEvent(
                    type="tool_failed",
                    session_id=state.session_id,
                    run_id=state.run_id,
                    tool_name=tool_name,
                    error=api_error(
                        decision.error_code,
                        decision.message,
                        detail={"step_id": step_id, "recovery_action": decision.action},
                        recoverable=decision.retryable,
                    ).model_dump(mode="json"),
                    payload={
                        "call_id": call.call_id,
                        "step_id": step_id,
                        "latency_ms": result.latency_ms or latency_ms,
                        "retry_count": retry_count,
                        "code": decision.error_code,
                        "recovery_action": decision.action,
                        "contract": contract_summary(result.contract),
                        "post_tool_call": post_tool_call,
                    },
                )
            )
            if self.tool_history is not None:
                self.tool_history.record_end(
                    state.run_id,
                    call.call_id,
                    tool_name,
                    "failed",
                    result.latency_ms or latency_ms,
                    error=decision.message,
                    user_id=state.user_id,
                    session_id=state.session_id,
                    output_summary=_output_summary(result),
                    audit_payload=result.audit_payload,
                    raw_data_ref=result.raw_data_ref,
                )
            _append_tool_trace_event(
                trace_store,
                trace_id=trace_id,
                state=state,
                node_name=node_name or "tool_executor",
                event_type="tool_failed",
                canonical_event="tool.failed",
                status="failed",
                capability=capability,
                tool_name=tool_name,
                call_id=call.call_id,
                step_id=step_id,
                span_id=tool_span_id,
                latency_ms=result.latency_ms or latency_ms,
                retry_count=retry_count,
                risk_gate=risk_decision.risk_summary(),
                idempotency=risk_decision.idempotency_summary(),
                input_summary=_input_summary(tool_input),
                output_summary=_output_summary(result),
                provider=_provider_name(result.data or {}),
                model=_model_name(result.data or {}),
                error_code=decision.error_code,
                error_message=decision.message,
                recovery_action=decision.action,
            )
        return result

    def _run_with_retry(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolContext,
        *,
        step_id: str,
        preserve_success_after_cancel: bool = False,
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
            if not self.execution_policy.retry.should_retry(error_code, failed_attempts):
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

    def _run_once(self, tool_name: str, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
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
    retry_count: int | None = None,
    risk_gate: dict[str, Any] | None = None,
    idempotency: dict[str, Any] | None = None,
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
                risk_gate=risk_gate,
                idempotency=idempotency,
            ),
            error=error,
        )
    )


def _tool_trace_attributes(
    *,
    call_id: str,
    step_id: str,
    retry_count: int | None,
    risk_gate: dict[str, Any] | None,
    idempotency: dict[str, Any] | None,
) -> dict[str, Any]:
    attributes: dict[str, Any] = {
        "tool_call_id": call_id,
        "step_id": step_id,
    }
    if retry_count is not None:
        attributes["retry_count"] = retry_count
    if risk_gate:
        attributes["risk_gate"] = risk_gate.get("level")
        attributes["side_effect_level"] = risk_gate.get("side_effect_level")
        attributes["requires_confirmation"] = risk_gate.get("requires_confirmation")
    if idempotency:
        attributes["idempotency_present"] = idempotency.get("present")
        attributes["idempotency_required"] = idempotency.get("required")
        attributes["idempotency_duplicate_suppressed"] = idempotency.get("duplicate_suppressed")
        attributes["idempotency_status"] = idempotency.get("status")
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


def _preserve_success_after_cancel(risk_decision: Any) -> bool:
    if risk_decision.level == "soft_gate":
        return True
    return bool(risk_decision.level == "hard_gate" and risk_decision.enabled and risk_decision.allow_execute)


def _capability_name(tool_name: str, step: TaskStep | None) -> str:
    if step is not None:
        action_map = {
            "understand_image": "image_understanding",
            "understand_video": "video_understanding",
            "search_web": "web_search",
            "search_product": "product_search",
            "compare_price": "price_compare",
            "generate_image": "image_generation",
            "render_3d": "render_3d",
            "retrieve_memory": "memory_retrieval",
            "save_memory": "memory_save",
        }
        if step.action in action_map:
            return action_map[step.action]
    tool_map = {
        "vision_understanding": "image_understanding",
        "video_understanding": "video_understanding",
        "web_search": "web_search",
        "product_search": "product_search",
        "price_compare": "price_compare",
        "image_generation": "image_generation",
        "render_3d": "render_3d",
        "memory_retrieval": "memory_retrieval",
        "memory_save": "memory_save",
    }
    return tool_map.get(tool_name, tool_name)


def _cancelled_tool_result(tool_name: str, *, latency_ms: int) -> ToolResult:
    return ToolResult(
        tool_name=tool_name,
        success=False,
        error=f"{CANCELLATION_ERROR_CODE}: {DEFAULT_CANCELLATION_MESSAGE}",
        data={"cancelled": True},
        latency_ms=latency_ms,
    )


def _bind_runtime_identity(tool_name: str, tool_input: dict[str, Any], state: AgentState) -> dict[str, Any]:
    """Bind memory ownership to the authenticated runtime state, not model arguments."""

    if tool_name not in {
        "memory",
        "memory_retrieval",
        "memory_save",
        "memory_media_ingest",
        "memory_ingest_status",
    }:
        return tool_input
    return {
        **tool_input,
        "user_id": state.user_id,
        "session_id": state.session_id,
    }


def _record_provider_budget_call(
    budget: ProviderCallBudget,
    *,
    run_id: str,
    capability: str,
    tool_input: dict[str, Any],
    result: ToolResult,
    latency_ms: int,
) -> None:
    data = result.data or {}
    budget.record_call(
        run_id=run_id,
        capability=capability,
        provider=_provider_name(data) or _provider_name(tool_input),
        model=_model_name(data),
        estimated_cost=_estimated_cost(data),
        cost_unit=_cost_unit(data),
        input_size_bytes=_input_size_bytes(tool_input),
        latency_ms=latency_ms,
        status="succeeded" if result.success else "failed",
    )


def _provider_name(payload: dict[str, Any]) -> str | None:
    value = payload.get("provider")
    return value if isinstance(value, str) and value else None


def _model_name(payload: dict[str, Any]) -> str | None:
    value = payload.get("model")
    return value if isinstance(value, str) and value else None


def _estimated_cost(payload: dict[str, Any]) -> float | None:
    value = payload.get("estimated_cost", payload.get("cost_estimate"))
    if isinstance(value, int | float) and value >= 0:
        return float(value)
    return None


def _cost_unit(payload: dict[str, Any]) -> str | None:
    value = payload.get("cost_unit")
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


def _side_effect_policy_for_registered_tool(
    registry: ToolRegistry,
    tool_name: str,
) -> ToolSideEffectPolicy | None:
    for spec in registry.list_specs():
        if spec.name != tool_name:
            continue
        if spec.policy is None:
            return None
        view = ToolPolicyInterpreter().view_for_spec(spec)
        return ToolSideEffectPolicy(
            level=view.side_effect_level,
            requires_confirmation=view.requires_confirmation,
            description=view.description,
            confirmation_kind=view.confirmation_kind,
            compensation_hint=view.compensation_hint,
        )
    return None


def _idempotency_required_for_registered_tool(
    registry: ToolRegistry,
    tool_name: str,
) -> bool | None:
    for spec in registry.list_specs():
        if spec.name != tool_name:
            continue
        if spec.policy is None:
            return None
        view = ToolPolicyInterpreter().view_for_spec(spec)
        return view.idempotency_required
    return None
