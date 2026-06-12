"""Tool execution service used by workflows and LangGraph nodes."""

from time import perf_counter
from typing import Any

from multimodal_agent.agent.state import AgentState
from multimodal_agent.agent.recovery import RecoveryPolicy
from multimodal_agent.schemas.api import api_error
from multimodal_agent.schemas.events import AgentEvent
from multimodal_agent.schemas.planning import TaskStep
from multimodal_agent.schemas.tools import ToolResult
from multimodal_agent.services.event_sink import EventSink
from multimodal_agent.services.tool_history import ToolHistoryStore
from multimodal_agent.services.trace_store import TraceEvent, TraceStore, sanitize_trace_value
from multimodal_agent.tools.base import ToolContext
from multimodal_agent.tools.registry import ToolRegistry, create_default_registry


class ToolExecutor:
    """Run tools through the registry and update AgentState records."""

    def __init__(
        self,
        registry: ToolRegistry | None = None,
        tool_history: ToolHistoryStore | None = None,
        event_sink: EventSink | None = None,
        recovery_policy: RecoveryPolicy | None = None,
    ) -> None:
        self.registry = registry or create_default_registry()
        self.tool_history = tool_history
        self.event_sink = event_sink
        self.recovery_policy = recovery_policy or RecoveryPolicy()

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
        call = state.add_tool_call(tool_name, tool_input)
        self._emit(
            AgentEvent(
                type="tool_started",
                session_id=state.session_id,
                run_id=state.run_id,
                tool_name=tool_name,
                payload={"call_id": call.call_id, "step_id": step_id},
            )
        )
        if self.tool_history is not None:
            self.tool_history.record_start(state.run_id, call.call_id, tool_name, tool_input)
        started_at = perf_counter()
        try:
            result = self.registry.run(
                tool_name,
                tool_input,
                ToolContext(run_id=state.run_id, user_id=state.user_id, session_id=state.session_id),
            )
        except Exception as exc:  # pragma: no cover - registry boundary
            result = ToolResult(tool_name=tool_name, success=False, error=str(exc))
        latency_ms = int((perf_counter() - started_at) * 1000)
        if result.latency_ms is None:
            result.latency_ms = latency_ms

        if result.success:
            state.complete_tool_call(call.call_id, result)
            self._emit(
                AgentEvent(
                    type="tool_finished",
                    session_id=state.session_id,
                    run_id=state.run_id,
                    tool_name=tool_name,
                    output_ref=result.output_ref,
                    payload={"call_id": call.call_id, "step_id": step_id, "latency_ms": result.latency_ms or latency_ms},
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
                )
        else:
            decision = self.recovery_policy.decide(result, step)
            result.error = decision.message
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
                        "code": decision.error_code,
                        "recovery_action": decision.action,
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
                )
            if trace_store is not None and trace_id is not None:
                trace_store.append(
                    TraceEvent(
                        trace_id=trace_id,
                        run_id=state.run_id,
                        node_name=node_name or "tool_executor",
                        event_type="tool_failed",
                        tool_name=tool_name,
                        error={
                            "code": decision.error_code,
                            "message": sanitize_trace_value(decision.message),
                            "recovery_action": decision.action,
                            "step_id": step_id,
                        },
                    )
                )
        return result

    def _emit(self, event: AgentEvent) -> None:
        if self.event_sink is not None:
            self.event_sink.emit(event)
