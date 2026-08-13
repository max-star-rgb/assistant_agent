"""Project canonical runtime facts into delivery and observability events."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from collections.abc import Callable
from typing import Any, Literal

from assistant_agent.api.models import api_error, api_error_from_agent_error
from assistant_agent.observability.trace_store import (
    TraceEvent,
    TraceStore,
    new_span_id,
    sanitize_trace_value,
)
from assistant_agent.runtime.event_sink import EventSink
from assistant_agent.runtime.product_event_projector import (
    ProductEventProjector,
    ProductProgressFact,
    RunCancelledProductFact,
    RunFailedProductFact,
    RunFinalProductFact,
    RunStartedProductFact,
    RuntimeProductFact,
    ToolStartedProductFact,
    ToolTerminalProductFact,
    emit_product_fact,
    new_runtime_product_fact_id,
)
from assistant_agent.runtime.state import AgentState
RunTerminalStatus = Literal["completed", "failed", "cancelled"]
AssistantStepTraceType = Literal["assistant_output", "tool_observation"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class RunStartedFact:
    """One run start shared by delivery and trace projections."""

    state: AgentState
    parent_span_id: str | None
    execution_engine: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(frozen=True)
class AssistantStepFact:
    """One ReAct decision or observation shared by display and trace."""

    state: AgentState
    trace_id: str | None
    node_name: str
    decision_trace: dict[str, Any]
    trace_event_type: AssistantStepTraceType
    canonical_event: str
    observation_type: Literal["event"] | None
    observation_scope: Literal["runtime", "iteration"]
    status: str | None = None
    tool_name: str | None = None
    output_summary: dict[str, Any] = field(default_factory=dict)
    attributes: dict[str, Any] = field(default_factory=dict)
    trace_error: dict[str, Any] | None = None
    occurred_at: datetime = field(default_factory=_now)


@dataclass(frozen=True)
class ToolStartedFact:
    """One governed tool invocation accepted for execution."""

    state: AgentState
    trace_id: str | None
    node_name: str
    capability: str
    tool_name: str
    tool_call_id: str
    step_id: str
    span_id: str
    tool_contract: dict[str, Any]
    input_summary: dict[str, Any]
    pre_tool_call: dict[str, Any]
    progress_message: str | None = None
    occurred_at: datetime = field(default_factory=_now)


@dataclass(frozen=True)
class ToolRetryFact:
    """One retry scheduled inside a governed logical tool invocation."""

    state: AgentState
    trace_id: str | None
    node_name: str
    capability: str
    tool_name: str
    tool_call_id: str
    step_id: str
    parent_span_id: str
    failed_attempt: int
    next_attempt: int
    max_attempts: int
    error_code: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(frozen=True)
class RunTerminalFact:
    """One run terminal committed before trace summary and delivery."""

    state: AgentState
    terminal_status: RunTerminalStatus
    latency_ms: int
    occurred_at: datetime = field(default_factory=_now)


@dataclass(frozen=True)
class ToolTerminalFact:
    """One governed tool invocation committed to its terminal state."""

    state: AgentState
    trace_id: str | None
    node_name: str
    capability: str
    tool_name: str
    tool_call_id: str
    step_id: str
    span_id: str
    success: bool
    status: str
    latency_ms: int
    reported_latency_ms: int | None
    retry_count: int
    tool_contract: dict[str, Any]
    input_summary: dict[str, Any]
    output_summary: dict[str, Any]
    post_tool_call: dict[str, Any]
    contract_summary: dict[str, Any] | None
    output_ref: str | None = None
    provider: str | None = None
    model: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    trace_error_message: str | None = None
    recovery_action: str | None = None
    delivery_recovery_action: str | None = None
    retryable: bool = False
    agent_error_detail: dict[str, Any] | None = None
    attempt_count: int | None = None
    execution_retry_count: int | None = None
    retry_exhausted: bool = False
    occurred_at: datetime = field(default_factory=_now)


class RuntimeEventPublisher:
    """Publish one runtime fact through its independent public projections."""

    def __init__(
        self,
        *,
        event_sink: EventSink | None = None,
        trace_store: TraceStore | None,
        product_fact_writer: Callable[[Any], None] | None = None,
        product_event_projector: ProductEventProjector | None = None,
    ) -> None:
        if event_sink is not None and product_event_projector is not None:
            raise ValueError("pass event_sink or product_event_projector, not both")
        self.product_fact_writer = product_fact_writer
        self.product_event_projector = product_event_projector or (
            ProductEventProjector(event_sink=event_sink)
            if event_sink is not None
            else None
        )
        self.trace_store = trace_store

    def record_run_started(self, fact: RunStartedFact) -> None:
        """Record the run-start trace projection in runtime order."""

        state = fact.state
        attributes = {
            "execution_engine": fact.execution_engine,
            "assistant_mode": state.request.assistant_mode,
        }
        self._append_trace(
            TraceEvent(
                trace_id=state.trace_id,
                run_id=state.run_id,
                user_id=state.user_id,
                session_id=state.session_id,
                node_name="runtime",
                event_type="observability",
                canonical_event="run.started",
                span_id=new_span_id(),
                parent_span_id=fact.parent_span_id,
                status="started",
                attributes=attributes,
                created_at=fact.occurred_at,
            )
        )

    def deliver_run_started(self, fact: RunStartedFact) -> None:
        """Deliver the run-start projection in stream order."""

        state = fact.state
        self._publish_product_fact(
            RunStartedProductFact(
                fact_id=new_runtime_product_fact_id("run_started"),
                session_id=state.session_id,
                run_id=state.run_id,
                user_id=state.user_id,
                agent_id=state.agent_id,
                trace_id=state.trace_id,
                occurred_at=fact.occurred_at,
            )
        )

    def record_run_terminal(self, fact: RunTerminalFact) -> None:
        """Record the trace projection before the terminal trace summary."""

        state = fact.state
        latest_error = _latest_state_error(state)
        canonical_event = {
            "completed": "run.completed",
            "failed": "run.failed",
            "cancelled": "run.cancelled",
        }[fact.terminal_status]
        workflow_outcome = state.request.metadata.get(
            "_trusted_workflow_work_item_outcome"
        )
        workflow_attributes = (
            {
                "workflow_work_item_status": workflow_outcome.get("status"),
                "workflow_work_item_error_code": workflow_outcome.get(
                    "error_code"
                ),
            }
            if isinstance(workflow_outcome, dict)
            else {}
        )
        self._append_trace(
            TraceEvent(
                trace_id=state.trace_id,
                run_id=state.run_id,
                user_id=state.user_id,
                session_id=state.session_id,
                node_name="runtime",
                event_type="observability",
                canonical_event=canonical_event,
                span_id=new_span_id(),
                status=fact.terminal_status,
                latency_ms=fact.latency_ms,
                attributes={
                    "tool_count": len(state.tool_calls),
                    "error_count": len(state.errors),
                    "response_present": state.response is not None,
                    **workflow_attributes,
                },
                error=latest_error,
                created_at=fact.occurred_at,
            )
        )

    def deliver_run_terminal(
        self,
        fact: RunTerminalFact,
        *,
        publish_product: bool = True,
        final_fact_id: str | None = None,
    ) -> None:
        """Deliver the terminal projection after trace finalization."""

        state = fact.state
        if fact.terminal_status == "failed":
            self._publish_product_fact(
                RunFailedProductFact(
                    fact_id=new_runtime_product_fact_id("run_failed"),
                    session_id=state.session_id,
                    run_id=state.run_id,
                    error=(
                        api_error_from_agent_error(state.errors[-1])
                        if state.errors
                        else api_error("TASK_FAILED", "Agent run failed.")
                    ),
                    occurred_at=fact.occurred_at,
                )
            )
            return
        if fact.terminal_status == "cancelled":
            self._publish_product_fact(
                RunCancelledProductFact(
                    fact_id=new_runtime_product_fact_id("run_cancelled"),
                    session_id=state.session_id,
                    run_id=state.run_id,
                    error=(
                        api_error_from_agent_error(state.errors[-1])
                        if state.errors
                        else api_error(
                            "AGENT_RUN_CANCELLED",
                            "Agent run cancelled.",
                        )
                    ),
                    occurred_at=fact.occurred_at,
                )
            )
            return
        if publish_product:
            self._publish_product_fact(
                RunFinalProductFact(
                    fact_id=final_fact_id or new_runtime_product_fact_id("run_final"),
                    session_id=state.session_id,
                    run_id=state.run_id,
                    text=state.response.message if state.response else "",
                    occurred_at=fact.occurred_at,
                )
            )
        if publish_product:
            response_text = state.response.message if state.response else ""
            self._append_trace(
                TraceEvent(
                    trace_id=state.trace_id,
                    run_id=state.run_id,
                    user_id=state.user_id,
                    session_id=state.session_id,
                    node_name="runtime",
                    event_type="observability",
                    canonical_event="response.delivered",
                    observation_type="span",
                    observation_name="response.delivered",
                    span_id=new_span_id(),
                    status="succeeded",
                    attributes={
                        "source": "runtime_final_response",
                        "message_present": bool(response_text),
                        "message_chars": len(response_text),
                    },
                    output_summary={
                        "response": {
                            "message_present": bool(response_text),
                            "message_chars": len(response_text),
                            "source": "runtime_final_response",
                        }
                    },
                    created_at=fact.occurred_at,
                )
            )

    def publish_tool_started(self, fact: ToolStartedFact) -> None:
        """Publish one tool-start fact to both projections."""

        if fact.trace_id is not None:
            self._append_trace(
                TraceEvent(
                    trace_id=fact.trace_id,
                    run_id=fact.state.run_id,
                    user_id=fact.state.user_id,
                    session_id=fact.state.session_id,
                    node_name=fact.node_name,
                    event_type="observability",
                    canonical_event="tool.started",
                    observation_scope="iteration",
                    span_id=fact.span_id,
                    capability=fact.capability,
                    tool_name=fact.tool_name,
                    status="started",
                    input_summary=fact.input_summary,
                    attributes=_compact(
                        {
                            "tool_call_id": fact.tool_call_id,
                            "step_id": fact.step_id,
                            "retry_count": 0,
                            "tool_reported_latency_ms": None,
                            "tool_category": fact.tool_contract.get("category"),
                            "content_export_policy": fact.tool_contract.get(
                                "trace_content_policy"
                            ),
                        }
                    ),
                    created_at=fact.occurred_at,
                )
            )
        self._publish_product_fact(
            ToolStartedProductFact(
                fact_id=new_runtime_product_fact_id("tool_started"),
                session_id=fact.state.session_id,
                run_id=fact.state.run_id,
                tool_name=fact.tool_name,
                tool_call_id=fact.tool_call_id,
                step_id=fact.step_id,
                text=fact.progress_message,
                pre_tool_call=fact.pre_tool_call,
                occurred_at=fact.occurred_at,
            )
        )

    def record_tool_retry(self, fact: ToolRetryFact) -> None:
        """Record a failed execution attempt and the retry it scheduled."""

        if fact.trace_id is None:
            return
        common = {
            "tool_call_id": fact.tool_call_id,
            "step_id": fact.step_id,
            "failed_attempt": fact.failed_attempt,
            "next_attempt": fact.next_attempt,
            "max_attempts": fact.max_attempts,
            "execution_retry_count": fact.next_attempt - 1,
        }
        self._append_trace(
            TraceEvent(
                trace_id=fact.trace_id,
                run_id=fact.state.run_id,
                user_id=fact.state.user_id,
                session_id=fact.state.session_id,
                node_name=fact.node_name,
                event_type="observability",
                canonical_event="tool.attempt.failed",
                observation_type="event",
                observation_scope="iteration",
                span_id=new_span_id(),
                parent_span_id=fact.parent_span_id,
                capability=fact.capability,
                tool_name=fact.tool_name,
                status="failed",
                error_code=fact.error_code,
                attributes=common,
                error={"code": fact.error_code, "message": "Tool execution attempt failed."},
                created_at=fact.occurred_at,
            )
        )
        self._append_trace(
            TraceEvent(
                trace_id=fact.trace_id,
                run_id=fact.state.run_id,
                user_id=fact.state.user_id,
                session_id=fact.state.session_id,
                node_name=fact.node_name,
                event_type="observability",
                canonical_event="tool.retry.scheduled",
                observation_type="event",
                observation_scope="iteration",
                span_id=new_span_id(),
                parent_span_id=fact.parent_span_id,
                capability=fact.capability,
                tool_name=fact.tool_name,
                status="scheduled",
                error_code=fact.error_code,
                attributes=common,
                created_at=fact.occurred_at,
            )
        )

    def publish_assistant_step(self, fact: AssistantStepFact) -> None:
        """Publish one ReAct step without reconstructing either projection."""

        trace_event = fact.decision_trace
        event_type = {
            "decision": "agent_trace_decision",
            "observation": "agent_trace_observation",
            "final_answer": "agent_trace_final_answer",
        }.get(str(trace_event.get("event")), "agent_trace_decision")
        self._publish_product_fact(
            ProductProgressFact(
                fact_id=new_runtime_product_fact_id("product_progress"),
                session_id=fact.state.session_id,
                run_id=fact.state.run_id,
                event_type=event_type,
                tool_name=(
                    trace_event.get("action")
                    if isinstance(trace_event.get("action"), str)
                    else None
                ),
                output_ref=(
                    trace_event.get("output_ref")
                    if isinstance(trace_event.get("output_ref"), str)
                    else None
                ),
                text=(
                    trace_event.get("answer")
                    if isinstance(trace_event.get("answer"), str)
                    else None
                ),
                error=trace_event.get("error"),
                payload={"decision_trace": trace_event},
                occurred_at=fact.occurred_at,
            )
        )
        if fact.trace_id is None:
            return
        self._append_trace(
            TraceEvent(
                trace_id=fact.trace_id,
                run_id=fact.state.run_id,
                user_id=fact.state.user_id,
                session_id=fact.state.session_id,
                node_name=fact.node_name,
                event_type=fact.trace_event_type,
                canonical_event=fact.canonical_event,
                observation_type=fact.observation_type,
                observation_scope=fact.observation_scope,
                tool_name=fact.tool_name,
                status=fact.status,
                output_summary=fact.output_summary,
                attributes=fact.attributes,
                error=_trace_error(fact.trace_error),
                created_at=fact.occurred_at,
            )
        )

    def publish_tool_terminal(self, fact: ToolTerminalFact) -> None:
        """Publish one committed tool terminal fact to both projections."""

        canonical_event = "tool.finished" if fact.success else "tool.failed"
        trace_error = _tool_trace_error(fact)
        if fact.trace_id is not None:
            self._append_trace(
                TraceEvent(
                    trace_id=fact.trace_id,
                    run_id=fact.state.run_id,
                    user_id=fact.state.user_id,
                    session_id=fact.state.session_id,
                    node_name=fact.node_name,
                    event_type="observability" if fact.success else "tool_failed",
                    canonical_event=canonical_event,
                    observation_type="span",
                    observation_scope="iteration",
                    span_id=fact.span_id,
                    capability=fact.capability,
                    tool_name=fact.tool_name,
                    provider=fact.provider,
                    model=fact.model,
                    status=fact.status,
                    latency_ms=fact.latency_ms,
                    error_code=fact.error_code,
                    input_summary=fact.input_summary,
                    output_summary=fact.output_summary,
                    attributes=_compact(
                        {
                            "tool_call_id": fact.tool_call_id,
                            "step_id": fact.step_id,
                            "retry_count": fact.retry_count,
                            "execution_retry_count": (
                                fact.execution_retry_count
                                if fact.execution_retry_count is not None
                                else fact.retry_count
                            ),
                            "attempt_count": (
                                fact.attempt_count
                                if fact.attempt_count is not None
                                else fact.retry_count + 1
                            ),
                            "retry_exhausted": fact.retry_exhausted,
                            "tool_reported_latency_ms": fact.reported_latency_ms,
                            "tool_category": fact.tool_contract.get("category"),
                            "content_export_policy": fact.tool_contract.get(
                                "trace_content_policy"
                            ),
                        }
                    ),
                    error=trace_error,
                    created_at=fact.occurred_at,
                )
            )
        self._publish_product_fact(
            ToolTerminalProductFact(
                fact_id=new_runtime_product_fact_id("tool_terminal"),
                session_id=fact.state.session_id,
                run_id=fact.state.run_id,
                tool_name=fact.tool_name,
                tool_call_id=fact.tool_call_id,
                step_id=fact.step_id,
                success=fact.success,
                latency_ms=fact.latency_ms,
                retry_count=fact.retry_count,
                post_tool_call=fact.post_tool_call,
                contract=fact.contract_summary,
                output_ref=fact.output_ref if fact.success else None,
                error=(
                    api_error(
                        fact.error_code or "tool_failed",
                        fact.error_message or "Tool failed.",
                        detail=(
                            fact.agent_error_detail
                            if fact.agent_error_detail is not None
                            else {
                                "step_id": fact.step_id,
                                "recovery_action": fact.recovery_action,
                            }
                        ),
                        recoverable=fact.retryable,
                    )
                    if not fact.success
                    else None
                ),
                code=fact.error_code,
                recovery_action=fact.delivery_recovery_action,
                occurred_at=fact.occurred_at,
            )
        )

    def _publish_product_fact(self, fact: RuntimeProductFact) -> None:
        if self.product_fact_writer is not None:
            emit_product_fact(self.product_fact_writer, fact)
            return
        if self.product_event_projector is not None:
            self.product_event_projector.project_fact(fact)

    def _append_trace(self, event: TraceEvent) -> None:
        if self.trace_store is not None:
            self.trace_store.append(event)


def _latest_state_error(state: AgentState) -> dict[str, Any] | None:
    if not state.errors:
        return None
    error = state.errors[-1]
    return {
        "code": error.details.get("code", "unknown_error"),
        "message": error.message,
        "source": error.source,
        "recovery_action": error.details.get("recovery_action"),
    }


def _tool_trace_error(fact: ToolTerminalFact) -> dict[str, Any] | None:
    if not (fact.error_code or fact.trace_error_message or fact.recovery_action):
        return None
    return {
        key: value
        for key, value in {
            "code": fact.error_code,
            "message": (
                sanitize_trace_value(fact.trace_error_message)
                if fact.trace_error_message
                else None
            ),
            "recovery_action": fact.recovery_action,
            "step_id": fact.step_id,
            "retry_count": fact.retry_count,
        }.items()
        if value is not None
    }


def _compact(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def _trace_error(error: dict[str, Any] | None) -> dict[str, Any] | None:
    if error is None:
        return None
    return {
        "code": error.get("code"),
        "message": sanitize_trace_value(str(error.get("message", ""))),
    }
