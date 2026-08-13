from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from assistant_agent.gateway.runtime_event_mapping import map_agent_progress_event
from assistant_agent.observability.agent_service_delivery import (
    AgentServiceDeliveryRegistry,
)
from assistant_agent.observability.trace_persistence import (
    close_trace_store,
    create_server_trace_store,
)
from assistant_agent.observability.trace_query import TraceQueryService
from assistant_agent.observability.trace_store import (
    InMemoryTraceStore,
    TraceEvent,
)
from assistant_agent.runtime.event_publisher import (
    RunStartedFact,
    RunTerminalFact,
    RuntimeEventPublisher,
    ToolStartedFact,
    ToolTerminalFact,
)
from assistant_agent.runtime.event_sink import ListEventSink
from assistant_agent.runtime.events import AgentEvent
from assistant_agent.runtime.output_models import (
    AssistantTextOutput,
    AssistantToolCall,
    AssistantTurnOutput,
)
from assistant_agent.runtime.requests import AgentResponse, UserRequest
from assistant_agent.runtime.state import AgentState


def _state() -> AgentState:
    return AgentState.from_request(
        UserRequest(
            user_id="user-sentinel",
            session_id="session-sentinel",
            text="request-sentinel",
        ),
        run_id="run-sentinel",
    )






class DeliveryAudit:
    def __init__(self) -> None:
        self.records: list[tuple[object, str, dict]] = []

    def append(self, delivery, event_type: str, **metadata) -> None:
        self.records.append((delivery, event_type, metadata))


@pytest.mark.core_invariant("OBS-001")
def test_assistant_output_accepts_only_text_or_tool_call() -> None:
    adapter = TypeAdapter(AssistantTurnOutput)

    assert adapter.validate_python(
        {"type": "text", "text": "text-sentinel"}
    ) == AssistantTextOutput(text="text-sentinel")
    assert adapter.validate_python(
        {
            "type": "tool_call",
            "tool_name": "probe_tool",
            "tool_input": {"value": "input-sentinel"},
        }
    ) == AssistantToolCall(
        tool_name="probe_tool",
        tool_input={"value": "input-sentinel"},
    )

    invalid_payloads = [
        {"type": "legacy-sentinel", "message": "message-sentinel"},
        {"type": "text", "text": ""},
        {"type": "text", "text": "text-sentinel", "tool_name": "probe_tool"},
        {"type": "tool_call", "tool_name": "", "tool_input": {}},
        {
            "type": "tool_call",
            "tool_name": "probe_tool",
            "tool_input": {},
            "text": "text-sentinel",
        },
    ]
    for payload in invalid_payloads:
        with pytest.raises(ValidationError):
            adapter.validate_python(payload)


@pytest.mark.core_invariant("OBS-001")
def test_run_facts_project_to_correlated_events() -> None:
    state = _state()
    state.set_response(AgentResponse(message="response-sentinel"))
    event_sink = ListEventSink()
    trace_store = InMemoryTraceStore()
    publisher = RuntimeEventPublisher(
        event_sink=event_sink,
        trace_store=trace_store,
    )

    started = RunStartedFact(
        state=state,
        parent_span_id="parent-span",
        execution_engine="engine-sentinel",
    )
    publisher.deliver_run_started(started)
    publisher.record_run_started(started)
    terminal = RunTerminalFact(
        state=state,
        terminal_status="completed",
        latency_ms=23,
    )
    publisher.record_run_terminal(terminal)
    publisher.deliver_run_terminal(terminal)

    traces = trace_store.list_by_run(state.run_id)
    assert [event.type for event in event_sink.events] == [
        "task_started",
        "final_response",
    ]
    assert [event.canonical_event for event in traces] == [
        "run.started",
        "run.completed",
        "response.delivered",
    ]
    assert {event.run_id for event in event_sink.events} == {state.run_id}
    assert {event.run_id for event in traces} == {state.run_id}
    assert {event.trace_id for event in traces} == {state.trace_id}
    assert event_sink.events[0].created_at == traces[0].created_at
    assert event_sink.events[1].created_at == traces[1].created_at
    assert event_sink.events[1].created_at == traces[2].created_at
    assert traces[2].attributes["source"] == "runtime_final_response"


@pytest.mark.core_invariant("OBS-001")
def test_tool_facts_share_span_and_timestamp() -> None:
    state = _state()
    call = state.add_tool_call(
        "probe_tool",
        {"value": "input-sentinel"},
    )
    event_sink = ListEventSink()
    trace_store = InMemoryTraceStore()
    publisher = RuntimeEventPublisher(
        event_sink=event_sink,
        trace_store=trace_store,
    )
    started = ToolStartedFact(
        state=state,
        trace_id=state.trace_id,
        node_name="node-sentinel",
        capability="capability-sentinel",
        tool_name="probe_tool",
        tool_call_id=call.tool_call_id,
        step_id="step-sentinel",
        span_id="span-sentinel",
        tool_contract={"category": "read"},
        input_summary={"field_names": ["value"]},
        pre_tool_call={"status": "accepted"},
    )
    terminal = ToolTerminalFact(
        state=state,
        trace_id=state.trace_id,
        node_name="node-sentinel",
        capability="capability-sentinel",
        tool_name="probe_tool",
        tool_call_id=call.tool_call_id,
        step_id="step-sentinel",
        span_id="span-sentinel",
        success=True,
        status="succeeded",
        latency_ms=17,
        reported_latency_ms=11,
        retry_count=0,
        tool_contract={"category": "read"},
        input_summary={"field_names": ["value"]},
        output_summary={"success": True},
        post_tool_call={"status": "succeeded"},
        contract_summary={},
        output_ref="output-sentinel",
    )

    publisher.publish_tool_started(started)
    publisher.publish_tool_terminal(terminal)

    traces = trace_store.list_by_run(state.run_id)
    assert [event.type for event in event_sink.events] == [
        "tool_started",
        "tool_finished",
    ]
    assert [event.canonical_event for event in traces] == [
        "tool.started",
        "tool.finished",
    ]
    assert traces[0].span_id == "span-sentinel"
    assert traces[1].span_id == "span-sentinel"
    assert event_sink.events[0].created_at == traces[0].created_at
    assert event_sink.events[1].created_at == traces[1].created_at


@pytest.mark.core_invariant("OBS-001")
def test_tool_failure_separates_delivery_and_trace_errors() -> None:
    state = _state()
    event_sink = ListEventSink()
    trace_store = InMemoryTraceStore()
    publisher = RuntimeEventPublisher(
        event_sink=event_sink,
        trace_store=trace_store,
    )

    publisher.publish_tool_terminal(
        ToolTerminalFact(
            state=state,
            trace_id=state.trace_id,
            node_name="node-sentinel",
            capability="capability-sentinel",
            tool_name="probe_tool",
            tool_call_id="call-sentinel",
            step_id="step-sentinel",
            span_id="span-sentinel",
            success=False,
            status="failed",
            latency_ms=17,
            reported_latency_ms=None,
            retry_count=0,
            tool_contract={"category": "read"},
            input_summary={"redacted": True},
            output_summary={"success": False},
            post_tool_call={"status": "failed"},
            contract_summary=None,
            error_code="error-code-sentinel",
            error_message="delivery-error-sentinel",
            trace_error_message="trace-error-sentinel",
            recovery_action="retry",
            delivery_recovery_action="retry",
            retryable=True,
        )
    )

    delivery = event_sink.events[0]
    trace = trace_store.list_by_run(state.run_id)[0]
    assert delivery.error["message"] == "delivery-error-sentinel"
    assert delivery.payload["recovery_action"] == "retry"
    assert trace.error["message"] == "trace-error-sentinel"
    assert trace.error["code"] == "error-code-sentinel"


@pytest.mark.core_invariant("OBS-001")
def test_trace_correlation_exists_before_work() -> None:
    mapped = map_agent_progress_event(
        AgentEvent(
            type="task_started",
            session_id="session-sentinel",
            run_id="run-sentinel",
            payload={"trace_id": "trace-sentinel"},
        )
    )

    assert mapped is not None
    assert mapped.type == "run.progress"
    assert mapped.payload["run_id"] == "run-sentinel"
    assert mapped.payload["trace_id"] == "trace-sentinel"


@pytest.mark.core_invariant("OBS-001")
def test_non_empty_trace_summary_keeps_optional_diagnostics_serializable() -> None:
    trace_store = InMemoryTraceStore()
    trace_store.append(
        TraceEvent(
            trace_id="trace-sentinel",
            run_id="run-sentinel",
            user_id="user-sentinel",
            session_id="session-sentinel",
            node_name="runtime",
            event_type="observability",
            canonical_event="run.completed",
            status="completed",
        )
    )

    summary = TraceQueryService(trace_store).trace_summary("trace-sentinel")

    assert summary is not None
    assert summary.budget_exceeded is False
    assert summary.retry_count == 0
    assert summary.model_dump(mode="json")["trace_id"] == "trace-sentinel"


@pytest.mark.core_invariant("OBS-001")
def test_server_trace_store_reads_persisted_trace_after_recreation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace_path = tmp_path / "trace.jsonl"
    first_store = create_server_trace_store(path=trace_path)
    first_store.append(
        TraceEvent(
            trace_id="trace-sentinel",
            run_id="run-sentinel",
            user_id="user-sentinel",
            session_id="session-sentinel",
            node_name="runtime",
            event_type="observability",
            canonical_event="run.completed",
            status="completed",
        )
    )
    assert close_trace_store(first_store, timeout=2.0) is True

    recreated_store = create_server_trace_store(path=trace_path)
    try:
        events = recreated_store.list_by_trace("trace-sentinel")
    finally:
        close_trace_store(recreated_store, timeout=2.0)

    assert [event.canonical_event for event in events] == ["run.completed"]


@pytest.mark.core_invariant("OBS-001")
def test_runtime_host_owns_runtime_and_trace_store_lifecycle_once() -> None:
    assert importlib.util.find_spec("assistant_agent.runtime.runtime_host") is not None

    from assistant_agent.runtime.runtime_host import RuntimeHost

    lifecycle: list[str] = []

    class Runtime:
        def close(self) -> bool:
            lifecycle.append("runtime")
            return True

    class TraceStore:
        def close(self, *, timeout: float) -> bool:
            assert timeout > 0
            lifecycle.append("trace_store")
            return True

    host = RuntimeHost(runtime=Runtime(), owned_trace_store=TraceStore())

    assert host.close(timeout=2.0) is True
    assert host.close(timeout=2.0) is True
    assert lifecycle == ["runtime", "trace_store"]






@pytest.mark.core_invariant("OBS-001")
def test_timeout_audit_keeps_all_correlation_ids() -> None:
    audit = DeliveryAudit()
    registry = AgentServiceDeliveryRegistry(audit_sink=audit)
    delivery = registry.accept(
        "session-sentinel",
        "chat-sentinel",
        expects_ack=False,
    )

    failed = registry.mark_failed(
        delivery.delivery_id,
        error_code="gateway_turn_timeout",
        run_id="run-sentinel",
        trace_id="trace-sentinel",
        runtime_status="pending_cancel",
        failure_source="gateway_turn_facade",
    )

    assert failed.run_id == "run-sentinel"
    assert failed.trace_id == "trace-sentinel"
    _, event_type, metadata = audit.records[-1]
    assert event_type == "failed"
    assert metadata == {
        "error_code": "gateway_turn_timeout",
        "runtime_status": "pending_cancel",
        "failure_source": "gateway_turn_facade",
    }
