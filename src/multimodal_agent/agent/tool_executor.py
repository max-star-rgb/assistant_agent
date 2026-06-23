"""Tool execution service used by workflows and LangGraph nodes."""

from time import perf_counter, sleep
from typing import Any

from multimodal_agent.agent.state import AgentState
from multimodal_agent.agent.recovery import RecoveryPolicy, classify_error
from multimodal_agent.schemas.api import api_error
from multimodal_agent.schemas.capability_output import build_capability_output_contract, contract_summary
from multimodal_agent.schemas.events import AgentEvent
from multimodal_agent.schemas.planning import TaskStep
from multimodal_agent.schemas.tools import ToolResult
from multimodal_agent.services.event_sink import EventSink
from multimodal_agent.services.provider_budget import ProviderCallBudget
from multimodal_agent.services.provider_errors import sanitize_error_message
from multimodal_agent.services.provider_policy import ProviderExecutionPolicy
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
        execution_policy: ProviderExecutionPolicy | None = None,
    ) -> None:
        self.registry = registry or create_default_registry()
        self.tool_history = tool_history
        self.event_sink = event_sink
        self.recovery_policy = recovery_policy or RecoveryPolicy()
        self.execution_policy = execution_policy or ProviderExecutionPolicy.from_env()

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
        capability = _capability_name(tool_name, step)
        budget = state.provider_budget
        budget_error = budget.check_before_call(
            capability=capability,
            provider=_provider_name(tool_input),
            estimated_cost=_estimated_cost(tool_input),
            input_size_bytes=_input_size_bytes(tool_input),
        )
        if budget_error is not None:
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
                contract=contract,
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
            if trace_store is not None and trace_id is not None:
                trace_store.append(
                    TraceEvent(
                        trace_id=trace_id,
                        run_id=state.run_id,
                        user_id=state.user_id,
                        session_id=state.session_id,
                        node_name=node_name or "tool_executor",
                        event_type="tool_failed",
                        capability=capability,
                        tool_name=tool_name,
                        status="failed",
                        error_code=budget_error.code,
                        input_summary=_input_summary(tool_input),
                        output_summary={"provider_budget": budget.summary()},
                        error={
                            "code": budget_error.code,
                            "message": sanitize_trace_value(budget_error.message),
                            "recovery_action": recovery_action,
                            "step_id": step_id,
                            "provider_budget": budget.summary(),
                        },
                    )
                )
            return result

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
            self.tool_history.record_start(
                state.run_id,
                call.call_id,
                tool_name,
                tool_input,
                user_id=state.user_id,
                session_id=state.session_id,
            )
        started_at = perf_counter()
        result, retry_count = self._run_with_retry(
            tool_name,
            tool_input,
            ToolContext(run_id=state.run_id, user_id=state.user_id, session_id=state.session_id),
        )
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
            state.complete_tool_call(call.call_id, result)
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
                )
            if trace_store is not None and trace_id is not None:
                trace_store.append(
                    TraceEvent(
                        trace_id=trace_id,
                        run_id=state.run_id,
                        user_id=state.user_id,
                        session_id=state.session_id,
                        node_name=node_name or "tool_executor",
                        event_type="tool_failed",
                        capability=capability,
                        tool_name=tool_name,
                        provider=_provider_name(result.data or {}),
                        model=_model_name(result.data or {}),
                        status="failed",
                        latency_ms=result.latency_ms or latency_ms,
                        error_code=decision.error_code,
                        input_summary=_input_summary(tool_input),
                        output_summary=_output_summary(result),
                        error={
                            "code": decision.error_code,
                            "message": sanitize_trace_value(decision.message),
                            "recovery_action": decision.action,
                            "step_id": step_id,
                            "retry_count": retry_count,
                        },
                    )
                )
        return result

    def _run_with_retry(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> tuple[ToolResult, int]:
        failed_attempts = 0
        retry_count = 0
        while True:
            result = self._run_once(tool_name, tool_input, context)
            if result.success:
                return result, retry_count

            error_code = classify_error(result.error or "")
            failed_attempts += 1
            if not self.execution_policy.retry.should_retry(error_code, failed_attempts):
                return result, retry_count

            retry_count += 1
            if self.execution_policy.retry.backoff_seconds > 0:
                sleep(self.execution_policy.retry.backoff_seconds)

    def _run_once(self, tool_name: str, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        try:
            return self.registry.run(tool_name, tool_input, context)
        except Exception as exc:  # pragma: no cover - registry boundary
            return ToolResult(tool_name=tool_name, success=False, error=sanitize_error_message(exc))

    def _emit(self, event: AgentEvent) -> None:
        if self.event_sink is not None:
            self.event_sink.emit(event)


def _capability_name(tool_name: str, step: TaskStep | None) -> str:
    if step is not None:
        action_map = {
            "understand_image": "image_understanding",
            "understand_video": "video_understanding",
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
        "product_search": "product_search",
        "price_compare": "price_compare",
        "image_generation": "image_generation",
        "render_3d": "render_3d",
        "memory_retrieval": "memory_retrieval",
        "memory_save": "memory_save",
    }
    return tool_map.get(tool_name, tool_name)


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
    return {
        "success": result.success,
        "output_ref": result.output_ref,
        "item_count": len(data.get("items", [])) if isinstance(data.get("items"), list) else None,
        "error_code": classify_error(result.error or "") if result.error else None,
    }
