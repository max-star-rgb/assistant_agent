"""Stable dual-projection contracts for canonical runtime lifecycle facts."""

from assistant_agent.observability.trace_store import InMemoryTraceStore
from assistant_agent.runtime.event_publisher import (
    AssistantStepFact,
    RunStartedFact,
    RunTerminalFact,
    RuntimeEventPublisher,
    ToolRetryFact,
    ToolStartedFact,
    ToolTerminalFact,
)
from assistant_agent.runtime.event_sink import ListEventSink
from assistant_agent.runtime.requests import AgentResponse, UserRequest
from assistant_agent.runtime.state import AgentState


def _state() -> AgentState:
    return AgentState.from_request(
        UserRequest(
            user_id="publisher-user",
            session_id="publisher-session",
            text="input-sentinel",
        ),
        run_id="publisher-run",
    )


def test_run_facts_project_to_correlated_trace_and_delivery_events() -> None:
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

    assert [event.type for event in event_sink.events] == [
        "task_started",
        "final_response",
    ]
    assert [
        event.canonical_event for event in trace_store.list_by_run(state.run_id)
    ] == ["run.started", "run.completed"]
    started_trace, terminal_trace = trace_store.list_by_run(state.run_id)
    assert event_sink.events[0].created_at == started_trace.created_at
    assert event_sink.events[1].created_at == terminal_trace.created_at
    assert started_trace.parent_span_id == "parent-span"
    assert started_trace.attributes["execution_engine"] == "engine-sentinel"
    assert terminal_trace.latency_ms == 23
    assert event_sink.events[1].text == "response-sentinel"


def test_tool_facts_project_once_with_shared_span_and_timestamp() -> None:
    state = _state()
    call = state.add_tool_call("tool-sentinel", {"value": "input-sentinel"})
    event_sink = ListEventSink()
    trace_store = InMemoryTraceStore()
    publisher = RuntimeEventPublisher(
        event_sink=event_sink,
        trace_store=trace_store,
    )
    started = ToolStartedFact(
        state=state,
        trace_id=state.trace_id,
        node_name="tool-node",
        capability="capability-sentinel",
        tool_name="tool-sentinel",
        tool_call_id=call.tool_call_id,
        step_id="step-sentinel",
        span_id="span-sentinel",
        tool_contract={"category": "read"},
        input_summary={"field_names": ["value"]},
        pre_tool_call={"status": "accepted"},
    )

    publisher.publish_tool_started(started)
    publisher.record_tool_retry(
        ToolRetryFact(
            state=state,
            trace_id=state.trace_id,
            node_name="tool-node",
            capability="capability-sentinel",
            tool_name="tool-sentinel",
            tool_call_id=call.tool_call_id,
            step_id="step-sentinel",
            parent_span_id="span-sentinel",
            failed_attempt=1,
            next_attempt=2,
            max_attempts=2,
            error_code="provider_timeout",
        )
    )
    publisher.publish_tool_terminal(
        ToolTerminalFact(
            state=state,
            trace_id=state.trace_id,
            node_name="tool-node",
            capability="capability-sentinel",
            tool_name="tool-sentinel",
            tool_call_id=call.tool_call_id,
            step_id="step-sentinel",
            span_id="span-sentinel",
            success=True,
            status="succeeded",
            latency_ms=17,
            reported_latency_ms=11,
            retry_count=1,
            tool_contract={"category": "read"},
            input_summary={"field_names": ["value"]},
            output_summary={"success": True},
            post_tool_call={"status": "succeeded"},
            contract_summary={},
            output_ref="output-sentinel",
            attempt_count=2,
            execution_retry_count=1,
        )
    )

    assert [event.type for event in event_sink.events] == [
        "tool_started",
        "tool_finished",
    ]
    trace_events = trace_store.list_by_run(state.run_id)
    assert [event.canonical_event for event in trace_events] == [
        "tool.started",
        "tool.attempt.failed",
        "tool.retry.scheduled",
        "tool.finished",
    ]
    assert trace_events[1].parent_span_id == "span-sentinel"
    assert trace_events[2].parent_span_id == "span-sentinel"
    assert trace_events[3].span_id == "span-sentinel"
    assert event_sink.events[0].created_at == trace_events[0].created_at
    assert event_sink.events[1].created_at == trace_events[3].created_at
    assert event_sink.events[1].output_ref == "output-sentinel"
    assert trace_events[3].attributes["attempt_count"] == 2
    assert trace_events[3].attributes["execution_retry_count"] == 1


def test_tool_failure_keeps_delivery_error_separate_from_trace_summary() -> None:
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
            node_name="tool-node",
            capability="capability-sentinel",
            tool_name="tool-sentinel",
            tool_call_id="call-sentinel",
            step_id="step-sentinel",
            span_id="span-sentinel",
            success=False,
            status="failed",
            latency_ms=17,
            reported_latency_ms=None,
            retry_count=1,
            tool_contract={"category": "read"},
            input_summary={"redacted": True},
            output_summary={"success": False},
            post_tool_call={"status": "failed"},
            contract_summary=None,
            error_code="failure-sentinel",
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
    assert "contract" not in delivery.payload
    assert trace.error["message"] == "trace-error-sentinel"


def test_assistant_step_fact_projects_display_and_trace_from_one_payload() -> None:
    state = _state()
    event_sink = ListEventSink()
    trace_store = InMemoryTraceStore()
    publisher = RuntimeEventPublisher(
        event_sink=event_sink,
        trace_store=trace_store,
    )
    decision_trace = {
        "iteration": 1,
        "event": "decision",
        "action": "tool-sentinel",
        "action_input": {"value": "input-sentinel"},
    }

    publisher.publish_assistant_step(
        AssistantStepFact(
            state=state,
            trace_id=state.trace_id,
            node_name="assistant-loop",
            decision_trace=decision_trace,
            trace_event_type="assistant_output",
            canonical_event="assistant.output",
            observation_type=None,
            observation_scope="runtime",
            status="tool_call",
            tool_name="tool-sentinel",
            output_summary={"output_type": "tool_call"},
            attributes={"iteration": 1},
        )
    )

    delivery = event_sink.events[0]
    trace = trace_store.list_by_run(state.run_id)[0]
    assert delivery.type == "agent_trace_decision"
    assert delivery.payload["decision_trace"] == decision_trace
    assert delivery.created_at == trace.created_at
    assert trace.canonical_event == "assistant.output"
    assert trace.tool_name == "tool-sentinel"
