from __future__ import annotations

from typing import ClassVar

import pytest
from pydantic import ValidationError, model_validator

from assistant_agent.observability.trace_store import InMemoryTraceStore
from assistant_agent.runtime.action_validator import ActionValidator
from assistant_agent.runtime.event_sink import ListEventSink
from assistant_agent.runtime.output_models import AssistantToolCall
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.state import AgentState
from assistant_agent.runtime.tool_executor import ToolExecutor
from assistant_agent.runtime.tool_operation_barrier import SQLiteToolOperationStore
from assistant_agent.tools.base import ToolContext, ToolInputValidationError
from assistant_agent.tools.input_binding import RuntimeInputBinding
from assistant_agent.tools.models import RunToolCatalog, ToolResult
from assistant_agent.tools.observation import (
    ToolObservation,
    native_tool_observation_payload,
    observation_from_tool_result,
)
from tests.core.support import ProbeInput, ProbeTool, sealed_registry


class RuntimeProbeInput(ProbeInput):
    user_id: str
    session_id: str


class RuntimeProbeTool(ProbeTool):
    name = "runtime_probe_tool"
    input_schema = RuntimeProbeInput
    output_schema = RuntimeProbeInput
    runtime_input_bindings = (
        RuntimeInputBinding(
            field="user_id",
            source="runtime_identity",
            key="user_id",
        ),
        RuntimeInputBinding(
            field="session_id",
            source="runtime_identity",
            key="session_id",
        ),
    )

    def _run(
        self,
        input: RuntimeProbeInput,
        context: ToolContext,
    ) -> ToolResult:
        return ToolResult(
            tool_name=self.name,
            success=True,
            data=input.model_dump(),
        )


class CountingProbeInput(ProbeInput):
    validation_count: ClassVar[int] = 0

    @model_validator(mode="after")
    def count_validation(self) -> "CountingProbeInput":
        type(self).validation_count += 1
        return self


class CountingProbeTool(ProbeTool):
    name = "counting_probe_tool"
    input_schema = CountingProbeInput
    output_schema = CountingProbeInput


class LatencyProbeTool(ProbeTool):
    name = "latency_probe_tool"

    def _run(self, input: ProbeInput, context: ToolContext) -> ToolResult:
        return ToolResult(
            tool_name=self.name,
            success=True,
            data={"value": input.value},
            latency_ms=1,
        )


class WriteProbeTool(ProbeTool):
    name = "write_probe_tool"
    category = "write"


class InvocationProbeTool(ProbeTool):
    name = "invocation_probe_tool"

    def __init__(self) -> None:
        self.invocations = 0

    def _run(self, input: ProbeInput, context: ToolContext) -> ToolResult:
        self.invocations += 1
        return super()._run(input, context)


class ValidatedProbeTool(ProbeTool):
    name = "validated_probe_tool"

    def validate_call(self, input: ProbeInput) -> None:
        if input.value == "blocked-sentinel":
            raise ToolInputValidationError(
                "probe_rejected",
                "error-sentinel",
            )


def _request(
    *,
    user_id: str = "user-sentinel",
    session_id: str = "session-sentinel",
) -> UserRequest:
    return UserRequest(
        user_id=user_id,
        session_id=session_id,
        text="request-sentinel",
    )


@pytest.mark.core_invariant("TOOL-001")
def test_runtime_owned_fields_are_hidden_and_bound_per_run() -> None:
    tool = RuntimeProbeTool()
    registry = sealed_registry(tool)
    spec = registry.get_spec(tool.name)
    first_request = _request(
        user_id="user-a-sentinel",
        session_id="session-a-sentinel",
    )
    first_state = AgentState.from_request(first_request)
    decision = AssistantToolCall(
        tool_name=tool.name,
        tool_input={"value": "value-a-sentinel"},
    )
    validation = ActionValidator().validate(
        decision=decision,
        registry=registry,
        request=first_request,
        state=first_state,
    )

    first = ToolExecutor(registry=registry).run_tool(
        first_state,
        "step-a-sentinel",
        tool.name,
        decision.tool_input,
        validated_input=validation.validated_input,
    )
    second_state = AgentState.from_request(
        _request(
            user_id="user-b-sentinel",
            session_id="session-b-sentinel",
        )
    )
    second = ToolExecutor(registry=registry).run_tool(
        second_state,
        "step-b-sentinel",
        tool.name,
        {"value": "value-b-sentinel"},
    )

    assert set(spec.input_schema["properties"]) == {"value"}
    assert spec.input_schema["required"] == ["value"]
    assert validation.accepted is True
    assert first.data == {
        "value": "value-a-sentinel",
        "user_id": "user-a-sentinel",
        "session_id": "session-a-sentinel",
    }
    assert second.data == {
        "value": "value-b-sentinel",
        "user_id": "user-b-sentinel",
        "session_id": "session-b-sentinel",
    }


@pytest.mark.core_invariant("TOOL-001")
def test_model_cannot_submit_runtime_owned_fields() -> None:
    tool = RuntimeProbeTool()
    registry = sealed_registry(tool)
    request = _request()

    result = ActionValidator().validate(
        decision=AssistantToolCall(
            tool_name=tool.name,
            tool_input={
                "value": "value-sentinel",
                "user_id": "spoofed-sentinel",
            },
        ),
        registry=registry,
        request=request,
        state=AgentState.from_request(request),
    )

    assert result.accepted is False
    assert result.code == "runtime_owned_tool_input"


@pytest.mark.core_invariant("TOOL-001")
def test_available_catalog_is_the_execution_allowlist() -> None:
    registry = sealed_registry()
    request = _request()
    state = AgentState.from_request(request)
    decision = AssistantToolCall(
        tool_name=ProbeTool.name,
        tool_input={"value": "value-sentinel"},
    )
    state.run_tool_catalog = RunToolCatalog(available_tool_names=[])

    rejected = ActionValidator().validate(
        decision=decision,
        registry=registry,
        request=request,
        state=state,
    )
    state.run_tool_catalog = RunToolCatalog(
        available_tool_names=[ProbeTool.name]
    )
    accepted = ActionValidator().validate(
        decision=decision,
        registry=registry,
        request=request,
        state=state,
    )

    assert rejected.code == "tool_not_allowed_for_run"
    assert accepted.code == "accepted"


@pytest.mark.core_invariant("TOOL-001")
def test_validated_input_is_reused_by_executor() -> None:
    CountingProbeInput.validation_count = 0
    tool = CountingProbeTool()
    registry = sealed_registry(tool)
    request = _request()
    state = AgentState.from_request(request)
    decision = AssistantToolCall(
        tool_name=tool.name,
        tool_input={"value": "value-sentinel"},
    )

    validation = ActionValidator().validate(
        decision=decision,
        registry=registry,
        request=request,
        state=state,
    )
    result = ToolExecutor(registry=registry).run_tool(
        state,
        "step-sentinel",
        tool.name,
        decision.tool_input,
        validated_input=validation.validated_input,
    )

    assert validation.accepted is True
    assert result.success is True
    assert CountingProbeInput.validation_count == 1


@pytest.mark.core_invariant("TOOL-001")
def test_executor_reports_wall_latency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = LatencyProbeTool()
    registry = sealed_registry(tool)
    trace_store = InMemoryTraceStore()
    clock_values = iter((100.0, 107.45, 107.46))
    monkeypatch.setattr(
        "assistant_agent.runtime.tool_executor.perf_counter",
        lambda: next(clock_values),
    )
    state = AgentState.from_request(_request())

    ToolExecutor(registry=registry).run_tool(
        state,
        "step-sentinel",
        tool.name,
        {"value": "value-sentinel"},
        trace_store=trace_store,
        trace_id=state.trace_id,
    )

    terminal = next(
        event
        for event in trace_store.list_by_trace(state.trace_id)
        if event.canonical_event == "tool.finished"
    )
    assert terminal.latency_ms >= 7450
    assert terminal.attributes["tool_reported_latency_ms"] == 1


@pytest.mark.core_invariant("TOOL-001")
def test_write_execution_has_one_structured_terminal_result(tmp_path) -> None:
    tool = WriteProbeTool()
    registry = sealed_registry(tool)
    events = ListEventSink()
    state = AgentState.from_request(_request())

    result = ToolExecutor(
        registry=registry,
        event_sink=events,
        operation_store=SQLiteToolOperationStore(tmp_path / "operations.sqlite3"),
    ).run_tool(
        state,
        "step-sentinel",
        tool.name,
        {"value": "value-sentinel"},
        operation_scope_id="core-write-operation-sentinel",
        operation_thread_id="assistant:core-thread-sentinel",
    )
    terminal_events = [
        event
        for event in events.events
        if event.type in {"tool_finished", "tool_failed"}
    ]

    assert result.success is True
    assert result.data == {"value": "value-sentinel"}
    assert len(state.tool_results) == 1
    assert len(terminal_events) == 1
    assert terminal_events[0].type == "tool_finished"


@pytest.mark.core_invariant("TOOL-001")
def test_default_executor_invokes_registered_tool_once() -> None:
    tool = InvocationProbeTool()
    state = AgentState.from_request(_request())

    result = ToolExecutor(registry=sealed_registry(tool)).run_tool(
        state,
        "step-sentinel",
        tool.name,
        {"value": "value-sentinel"},
    )

    assert tool.invocations == 1
    assert result.data == {"value": "value-sentinel"}


@pytest.mark.core_invariant("TOOL-001")
def test_llm_selected_probe_is_not_filtered_by_request_text() -> None:
    registry = sealed_registry()
    request = _request()

    result = ActionValidator().validate(
        decision=AssistantToolCall(
            tool_name=ProbeTool.name,
            tool_input={"value": "value-sentinel"},
        ),
        registry=registry,
        request=request,
        state=AgentState.from_request(request),
    )

    assert result.accepted is True
    assert result.code == "accepted"


@pytest.mark.core_invariant("TOOL-001")
def test_tool_owned_validator_runs_without_tool_name_branch() -> None:
    tool = ValidatedProbeTool()
    registry = sealed_registry(tool)
    request = _request()

    result = ActionValidator().validate(
        decision=AssistantToolCall(
            tool_name=tool.name,
            tool_input={"value": "blocked-sentinel"},
        ),
        registry=registry,
        request=request,
        state=AgentState.from_request(request),
    )

    assert result.accepted is False
    assert result.code == "probe_rejected"


@pytest.mark.core_invariant("TOOL-001")
def test_success_observation_separates_status_summary_and_data() -> None:
    observation = observation_from_tool_result(
        ToolResult(
            tool_name=ProbeTool.name,
            success=True,
            model_observation={
                "summary": "summary-sentinel",
                "outcome": "success",
                "value": "value-sentinel",
            },
        )
    )
    payload = native_tool_observation_payload(
        observation.model_dump(mode="json")
    )

    assert payload["status"] == "succeeded"
    assert payload["summary"] == "summary-sentinel"
    assert payload["outcome"] == "success"
    assert payload["data"] == {"value": "value-sentinel"}
    assert "error" not in payload


@pytest.mark.core_invariant("TOOL-001")
def test_failed_observation_uses_structured_error() -> None:
    observation = observation_from_tool_result(
        ToolResult(
            tool_name=ProbeTool.name,
            success=False,
            error="error-sentinel",
            model_observation={
                "summary": "summary-sentinel",
                "errors": [
                    {
                        "code": "error-sentinel",
                        "message": "error-sentinel",
                        "recoverable": True,
                    }
                ],
            },
        )
    )
    payload = native_tool_observation_payload(
        observation.model_dump(mode="json")
    )

    assert payload["status"] == "failed"
    assert payload["summary"] == "summary-sentinel"
    assert payload["is_complete"] is False
    assert payload["error"] == {
        "code": "error-sentinel",
        "message": "error-sentinel",
        "retryable": True,
    }
    assert "outcome" not in payload
    assert "data" not in payload


@pytest.mark.core_invariant("TOOL-001")
def test_removed_observation_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ToolObservation.model_validate(
            {
                "tool_name": ProbeTool.name,
                "status": "failed",
                "summary": "summary-sentinel",
                "structured_output": {"value": "value-sentinel"},
                "error_code": "error-sentinel",
                "error_message": "error-sentinel",
                "next_step_hint": "retry",
            }
        )
