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
from assistant_agent.schemas.durable_tasks import TrustedTaskBinding
from assistant_agent.schemas.planning import TaskStep
from assistant_agent.schemas.realtime_cancellation import build_realtime_turn_cancellation_metadata
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.event_sink import EventSink
from assistant_agent.services.provider_budget import ProviderCallBudget
from assistant_agent.services.provider_errors import sanitize_error_detail, sanitize_error_message
from assistant_agent.services.provider_policy import ProviderExecutionPolicy
from assistant_agent.services.agent_service_entry import is_trusted_agent_service_request
from assistant_agent.services.tool_call_boundary import (
    build_post_tool_call_summary,
    build_pre_tool_call_summary,
)
from assistant_agent.services.tool_history import ToolHistoryStore
from assistant_agent.services.tool_policy import ToolPolicyInterpreter, ToolPolicyView
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
        tool_input = _bind_runtime_media_inputs(tool_name, tool_input, state)
        tool_input = _bind_durable_idempotency(
            tool_input,
            step_id=step_id,
            context_metadata=self.context_metadata,
        )
        policy_view = ToolPolicyInterpreter().view_for_spec(self.registry.get_spec(tool_name))
        risk_decision = evaluate_tool_risk(
            tool_name=tool_name,
            tool_input=tool_input,
            request=state.request,
            state=state,
            step_id=step_id,
            policy_view=policy_view,
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
                _policy_safe_input_summary(tool_input, policy_view),
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
                    output_summary=_policy_safe_output_summary(result, policy_view),
                    audit_payload=_policy_safe_audit_payload(result, policy_view),
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
                output_summary=_policy_safe_output_summary(result, policy_view),
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
                    output_summary=_policy_safe_output_summary(result, policy_view),
                    audit_payload=_policy_safe_audit_payload(result, policy_view),
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
                output_summary=_policy_safe_output_summary(result, policy_view),
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
                    error=_policy_safe_error(budget_error.message, policy_view),
                    user_id=state.user_id,
                    session_id=state.session_id,
                    output_summary=_policy_safe_output_summary(result, policy_view),
                    audit_payload=_policy_safe_audit_payload(result, policy_view),
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

        before_tool_execution = self.context_metadata.get("_before_tool_execution")
        if callable(before_tool_execution):
            before_tool_execution()
        try:
            context_metadata = {
                **{
                    key: value
                    for key, value in self.context_metadata.items()
                    if key != "_before_tool_execution"
                },
                "request_text": state.request.text or "",
                "request_metadata": dict(state.request.metadata),
            }
            if risk_decision.idempotency_key is not None:
                context_metadata["idempotency_key"] = risk_decision.idempotency_key
            if policy_view.timeout_s is not None:
                context_metadata["tool_execution"] = {
                    "timeout_s": policy_view.timeout_s,
                    "deadline_monotonic_s": monotonic() + policy_view.timeout_s,
                }
            result, retry_count = self._run_with_retry(
                tool_name,
                tool_input,
                ToolContext(
                    run_id=state.run_id,
                    user_id=state.user_id,
                    session_id=state.session_id,
                    metadata=context_metadata,
                    cancel_token=self.cancel_token,
                ),
                step_id=step_id,
                preserve_success_after_cancel=_preserve_success_after_cancel(risk_decision),
                max_retries=_effective_max_retries(
                    policy_view=policy_view,
                    risk_decision=risk_decision,
                    global_max_retries=self.execution_policy.retry.max_retries,
                ),
            )
            result = _mark_unknown_mutating_timeout(result, policy_view=policy_view)
        except AgentRunCancelled as exc:
            latency_ms = int((perf_counter() - started_at) * 1000)
            error_details = {
                **exc.details,
                "step_id": step_id,
                "tool_name": tool_name,
                "retryable": False,
            }
            error_details = build_realtime_turn_cancellation_metadata(
                error_details,
                phase="tool_running",
            )
            result = _cancelled_tool_result(
                tool_name,
                latency_ms=latency_ms,
                cancel_metadata=error_details,
            )
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
                    error=_policy_safe_error(DEFAULT_CANCELLATION_MESSAGE, policy_view),
                    user_id=state.user_id,
                    session_id=state.session_id,
                    output_summary=_policy_safe_output_summary(result, policy_view),
                    audit_payload=_policy_safe_audit_payload(result, policy_view),
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
                    output_summary=_policy_safe_output_summary(result, policy_view),
                    audit_payload=_policy_safe_audit_payload(result, policy_view),
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
                output_summary={
                    **_policy_safe_output_summary(result, policy_view),
                    **_deadline_trace_summary(policy_view=policy_view, result=result),
                },
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
                    error=_policy_safe_error(decision.message, policy_view),
                    user_id=state.user_id,
                    session_id=state.session_id,
                    output_summary=_policy_safe_output_summary(result, policy_view),
                    audit_payload=_policy_safe_audit_payload(result, policy_view),
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
                output_summary={
                    **_policy_safe_output_summary(result, policy_view),
                    **_deadline_trace_summary(policy_view=policy_view, result=result),
                },
                provider=_provider_name(result.data or {}),
                model=_model_name(result.data or {}),
                error_code=decision.error_code,
                error_message=_policy_safe_error(decision.message, policy_view),
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
            "fetch_web": "web_fetch",
            "search_image_by_image": "visual_image_search",
            "shopping_search": "shopping_search",
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
        "web_fetch": "web_fetch",
        "visual_image_search": "visual_image_search",
        "shopping_search": "shopping_search",
        "image_generation": "image_generation",
        "render_3d": "render_3d",
        "memory_retrieval": "memory_retrieval",
        "memory_save": "memory_save",
    }
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


def _bind_runtime_media_inputs(tool_name: str, tool_input: dict[str, Any], state: AgentState) -> dict[str, Any]:
    """Bind request-scoped media refs for tools without exposing them as model-visible facts."""

    if tool_name != "video_understanding":
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


def _policy_safe_input_summary(
    payload: dict[str, Any],
    policy_view: ToolPolicyView,
) -> dict[str, Any]:
    if not policy_view.redact_in_trace:
        return payload
    return {
        "redacted": True,
        "field_names": sorted(str(key) for key in payload),
        "field_count": len(payload),
        "input_size_bytes": _input_size_bytes(payload),
    }


def _policy_safe_output_summary(
    result: ToolResult,
    policy_view: ToolPolicyView,
) -> dict[str, Any]:
    if not policy_view.redact_in_trace:
        return _output_summary(result)
    data = result.data if isinstance(result.data, dict) else {}
    trace = result.trace_summary if isinstance(result.trace_summary, dict) else {}
    approved_summary = trace.get("summary")
    return {
        "redacted": True,
        "success": result.success,
        "output_ref": result.output_ref,
        "error_code": classify_error(result.error or "") if result.error else None,
        "summary": (
            sanitize_error_message(approved_summary)[:240]
            if isinstance(approved_summary, str) and approved_summary.strip()
            else None
        ),
        "data_field_names": sorted(str(key) for key in data),
        "trace_field_names": sorted(str(key) for key in trace),
        "result_size_bytes": len(str(data).encode("utf-8")),
    }


def _policy_safe_audit_payload(
    result: ToolResult,
    policy_view: ToolPolicyView,
) -> dict[str, Any] | None:
    payload = result.audit_payload
    if not policy_view.redact_in_trace or not isinstance(payload, dict):
        return payload
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


def _policy_safe_error(error: str | None, policy_view: ToolPolicyView) -> str | None:
    if error is None or not policy_view.redact_in_trace:
        return error
    return classify_error(error)


def _mark_unknown_mutating_timeout(
    result: ToolResult,
    *,
    policy_view: ToolPolicyView,
) -> ToolResult:
    if result.success or classify_error(result.error or "") != "provider_timeout":
        return result
    if policy_view.side_effect_level in {"none", "local_read", "external_read"}:
        return result
    return result.model_copy(
        update={
            "data": {
                **(result.data or {}),
                "status": "unknown_after_timeout",
                "side_effect_state": "unknown",
                "summary": "Tool timed out after a mutating request; commit status is unknown.",
            }
        },
        deep=True,
    )


def _effective_max_retries(
    *,
    policy_view: ToolPolicyView,
    risk_decision: Any,
    global_max_retries: int,
) -> int:
    """Return the retry ceiling after replay-safety and tool policy checks."""

    replay_safe = policy_view.side_effect_level in {"none", "local_read", "external_read"}
    if policy_view.idempotency_required and risk_decision.idempotency_key is not None:
        replay_safe = True
    if not replay_safe:
        return 0
    if policy_view.retry_count is None:
        return global_max_retries
    return min(policy_view.retry_count, global_max_retries)


def _deadline_trace_summary(
    *,
    policy_view: ToolPolicyView,
    result: ToolResult,
) -> dict[str, Any]:
    if policy_view.timeout_s is None:
        return {}
    trace_summary = result.trace_summary if isinstance(result.trace_summary, dict) else {}
    adapter_enforced = trace_summary.get("deadline_enforced") is True
    return {
        "timeout_s_declared": policy_view.timeout_s,
        "deadline_propagated": True,
        "deadline_enforcement": "adapter_reported" if adapter_enforced else "not_reported",
    }
