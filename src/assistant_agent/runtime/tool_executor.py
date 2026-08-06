"""Serial governed Tool execution used by workflows and assistant loops."""

from collections.abc import Callable
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
from assistant_agent.tools.capability_output import contract_summary
from assistant_agent.runtime.event_publisher import (
    RuntimeEventPublisher,
    ToolStartedFact,
    ToolTerminalFact,
    ToolRetryFact,
)
from assistant_agent.identity import RequestIdentity
from assistant_agent.runtime.planning_models import TaskStep
from assistant_agent.gateway.cancellation_models import (
    build_realtime_turn_cancellation_metadata,
)
from assistant_agent.tools.ids import (
    IMAGE_GENERATION_CAPABILITY,
    IMAGE_GENERATION_TOOL_NAME,
    LOAD_SKILL_TOOL_NAME,
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
    TraceStore,
    new_span_id,
    sanitize_trace_value,
)
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.input_binding import bind_runtime_tool_input
from assistant_agent.tools.plugins.registry_factory import create_default_registry
from assistant_agent.tools.registry import ToolRegistry


_VISUAL_TRACE_LINK_FIELDS = (
    "source_vision_trace_id",
    "source_vision_run_id",
    "source_vlm_span_id",
    "source_visual_record_id",
    "snapshot_sequence",
)


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
        progress_message: str | None = None,
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
        trace_content_policy = _trace_content_policy(tool)
        execution_summary = {
            "category": tool_spec.category,
            "trace_content_policy": trace_content_policy,
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
        publisher = RuntimeEventPublisher(
            event_sink=self.event_sink,
            trace_store=trace_store,
        )
        publisher.publish_tool_started(
            ToolStartedFact(
                state=state,
                trace_id=trace_id,
                node_name=effective_node_name,
                capability=capability,
                tool_name=tool_name,
                tool_call_id=call.tool_call_id,
                step_id=step_id,
                span_id=tool_span_id,
                tool_contract=execution_summary,
                input_summary=_policy_safe_input_summary(
                    bound_input,
                    content_policy=trace_content_policy,
                ),
                pre_tool_call=pre_tool_call,
                progress_message=progress_message,
            )
        )

        retry_count = 0
        before_tool_execution = self.context_metadata.get("_before_tool_execution")
        if callable(before_tool_execution):
            before_tool_execution()
        context = ToolContext(
            run_id=state.run_id,
            trace_id=trace_id or state.trace_id,
            trace_store=trace_store,
            parent_span_id=tool_span_id,
            user_id=state.user_id,
            session_id=state.session_id,
            skill_reference_grants=_loaded_skill_reference_grants(state),
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
                "latest_generated_image_id": (
                    _latest_generated_image_id(state)
                    or _agent_service_latest_generated_image_id(state)
                ),
            },
            cancel_token=self.cancel_token,
        )
        invocation_input: BaseModel | dict[str, Any] = bound_input
        if validated_input is not None and bound_input == normalized_input:
            invocation_input = validated_input
        elif validated_input is not None:
            invocation_input = tool.input_schema.model_validate(bound_input)
        max_execution_retries = _effective_max_retries(
            tool_spec=tool_spec,
            global_max_retries=self.execution_policy.retry.max_retries,
        )

        def record_retry(
            failed_attempt: int,
            next_attempt: int,
            error_code: str,
        ) -> None:
            publisher.record_tool_retry(
                ToolRetryFact(
                    state=state,
                    trace_id=trace_id,
                    node_name=effective_node_name,
                    capability=capability,
                    tool_name=tool_name,
                    tool_call_id=call.tool_call_id,
                    step_id=step_id,
                    parent_span_id=tool_span_id,
                    failed_attempt=failed_attempt,
                    next_attempt=next_attempt,
                    max_attempts=max_execution_retries + 1,
                    error_code=error_code,
                )
            )

        try:
            result, retry_count = self._run_with_retry(
                tool_name,
                invocation_input,
                context,
                step_id=step_id,
                preserve_success_after_cancel=tool_spec.category != "read",
                max_retries=max_execution_retries,
                on_retry=record_retry,
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
        tool_contract = _execution_summary(
            tool_spec,
            trace_content_policy=trace_content_policy,
        )

        error_code = None
        recovery_action = None
        retryable = False
        if result.success:
            state.complete_tool_call(call.tool_call_id, result)
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
        publisher.publish_tool_terminal(
            ToolTerminalFact(
                state=state,
                trace_id=trace_id,
                node_name=effective_node_name,
                capability=capability,
                tool_name=tool_name,
                tool_call_id=call.tool_call_id,
                step_id=step_id,
                span_id=tool_span_id,
                success=result.success,
                status=str(post_tool_call.get("status") or status),
                latency_ms=latency_ms,
                reported_latency_ms=reported_latency_ms,
                retry_count=retry_count,
                tool_contract=tool_contract,
                input_summary=_policy_safe_input_summary(
                    bound_input,
                    content_policy=trace_content_policy,
                ),
                output_summary=_policy_safe_output_summary(
                    result,
                    content_policy=trace_content_policy,
                ),
                post_tool_call=post_tool_call,
                contract_summary=contract_summary(result.contract),
                output_ref=result.output_ref if result.success else None,
                provider=_provider_name(result.data or {}),
                model=_model_name(result.data or {}),
                error_code=error_code,
                error_message=result.error if not result.success else None,
                trace_error_message=(
                    (
                        "Tool execution failed."
                        if trace_content_policy == "metadata_only"
                        else _policy_safe_error(result.error)
                    )
                    if not result.success
                    else None
                ),
                recovery_action=recovery_action,
                delivery_recovery_action=recovery_action,
                retryable=retryable,
                attempt_count=retry_count + 1,
                execution_retry_count=retry_count,
                retry_exhausted=(
                    not result.success
                    and max_execution_retries > 0
                    and retry_count >= max_execution_retries
                    and self.execution_policy.retry.is_retryable(
                        classify_error(result.error or "")
                    )
                ),
            )
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
        trace_content_policy = _trace_content_policy(self.registry.get(tool_name))
        tool_contract = _execution_summary(
            tool_spec,
            trace_content_policy=trace_content_policy,
        )
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
        RuntimeEventPublisher(
            event_sink=self.event_sink,
            trace_store=trace_store,
        ).publish_tool_terminal(
            ToolTerminalFact(
                state=state,
                trace_id=trace_id,
                node_name=node_name,
                capability=capability,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                step_id=step_id,
                span_id=tool_span_id,
                success=False,
                status="failed",
                latency_ms=latency_ms,
                reported_latency_ms=result.latency_ms,
                retry_count=0,
                tool_contract=tool_contract,
                input_summary=_policy_safe_input_summary(
                    tool_input,
                    content_policy=trace_content_policy,
                ),
                output_summary={"cancelled": True},
                post_tool_call=post_tool_call,
                contract_summary=None,
                error_code=CANCELLATION_ERROR_CODE,
                error_message=DEFAULT_CANCELLATION_MESSAGE,
                trace_error_message=DEFAULT_CANCELLATION_MESSAGE,
                recovery_action="cancelled",
                agent_error_detail={"step_id": step_id, "source": tool_name},
            )
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
        on_retry: Callable[[int, int, str], None] | None = None,
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
            error_code = classify_error(result.error or "")
            if (
                failed_attempts > max_retries
                or not self.execution_policy.retry.is_retryable(error_code)
            ):
                return result, retry_count
            if on_retry is not None:
                on_retry(failed_attempts, failed_attempts + 1, error_code)
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


def _loaded_skill_reference_grants(
    state: AgentState,
) -> dict[str, list[str]]:
    """Derive same-run reference grants from successful load_skill results."""

    grants: dict[str, list[str]] = {}
    for result in state.tool_results:
        if (
            not result.success
            or result.tool_name != LOAD_SKILL_TOOL_NAME
            or not isinstance(result.data, dict)
        ):
            continue
        skill_id = result.data.get("skill_id")
        reference_ids = result.data.get("reference_ids")
        if not isinstance(skill_id, str) or not isinstance(reference_ids, list):
            continue
        allowed = grants.setdefault(skill_id, [])
        for reference_id in reference_ids:
            if isinstance(reference_id, str) and reference_id not in allowed:
                allowed.append(reference_id)
    return grants


def _latest_generated_image_id(state: AgentState) -> str | None:
    """Return the latest successful same-run image generation ID."""

    for result in reversed(state.tool_results):
        if not result.success or result.tool_name != IMAGE_GENERATION_TOOL_NAME:
            continue
        for payload in (result.model_observation, result.data):
            if not isinstance(payload, dict):
                continue
            image_ids = payload.get("image_id")
            if not isinstance(image_ids, list):
                continue
            for image_id in reversed(image_ids):
                if isinstance(image_id, str) and image_id.strip():
                    return image_id.strip()
    return None


def _agent_service_latest_generated_image_id(state: AgentState) -> str | None:
    agent_service = state.request.metadata.get("agent_service")
    if not isinstance(agent_service, dict):
        return None
    image_id = agent_service.get("latest_generated_image_id")
    if not isinstance(image_id, str) or not image_id.strip():
        return None
    return image_id.strip()


def _policy_safe_input_summary(
    payload: dict[str, Any],
    *,
    content_policy: str = "default",
) -> dict[str, Any]:
    if content_policy == "metadata_only":
        return {
            "content_export_policy": "metadata_only",
            "redacted": True,
            "field_count": len(payload),
            "field_names": sorted(str(key) for key in payload),
        }
    if local_trace_content_enabled():
        safe = sanitize_error_detail(payload)
        return safe if isinstance(safe, dict) else {}
    return {
        "redacted": True,
        "field_names": sorted(str(key) for key in payload),
        "input_size_bytes": len(str(payload).encode("utf-8")),
    }


def _policy_safe_output_summary(
    result: ToolResult,
    *,
    content_policy: str = "default",
) -> dict[str, Any]:
    if content_policy == "metadata_only":
        data = result.data if isinstance(result.data, dict) else {}
        return {
            "content_export_policy": "metadata_only",
            "redacted": True,
            "success": result.success,
            "data_field_names": sorted(str(key) for key in data),
            "error_code": classify_error(result.error or "") if result.error else None,
            **_visual_trace_link_summary(result),
        }
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


def _visual_trace_link_summary(result: ToolResult) -> dict[str, Any]:
    trace_summary = result.trace_summary
    if not isinstance(trace_summary, dict):
        return {}
    values: dict[str, Any] = {}
    for key in _VISUAL_TRACE_LINK_FIELDS:
        value = trace_summary.get(key)
        if isinstance(value, str) and value:
            values[key] = value
        elif key == "snapshot_sequence" and isinstance(value, int) and not isinstance(value, bool):
            values[key] = value
    return values


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


def _execution_summary(
    tool_spec: ToolSpec,
    *,
    trace_content_policy: str = "default",
) -> dict[str, Any]:
    return {
        "category": tool_spec.category,
        "trace_content_policy": trace_content_policy,
    }


def _trace_content_policy(tool: object) -> str:
    value = getattr(tool, "trace_content_policy", "default")
    return "metadata_only" if value == "metadata_only" else "default"
