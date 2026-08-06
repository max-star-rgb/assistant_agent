from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import TypeAdapter, ValidationError

from assistant_agent.gateway.runtime_event_mapping import map_agent_progress_event
from assistant_agent.gateway.turn_facade import (
    GatewayTurnFacade,
    GatewayTurnRequest,
    GatewayTurnTimeout,
)
from assistant_agent.observability.agent_service_delivery import (
    AgentServiceDeliveryRegistry,
)
from assistant_agent.observability.otel_mapping import build_text_otel_span_specs
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


class HangingGatewayEndpoint:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.received: asyncio.Queue[dict] = asyncio.Queue()
        self.correlation_sent = asyncio.Event()

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.received.get()

    async def send(self, outbound: dict) -> None:
        self.sent.append(dict(outbound))
        if outbound.get("type") != "message.user":
            return
        payload = outbound["payload"]
        common = {
            "session_id": outbound["session_id"],
            "turn_id": payload["turn_id"],
            "run_id": payload["run_id"],
        }
        await self.received.put({"type": "run.started", **common})
        await self.received.put(
            {
                "type": "event.progress",
                **common,
                "payload": {
                    "agent_event_type": "task_started",
                    "trace_id": "trace-sentinel",
                },
            }
        )
        self.correlation_sent.set()


class GatewayManagerStub:
    def __init__(self, endpoint: HangingGatewayEndpoint) -> None:
        self.endpoint = endpoint

    async def acquire(self, **_kwargs):
        return SimpleNamespace(endpoint=self.endpoint)


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
    monkeypatch.setenv("ASSISTANT_AGENT_OTEL_EXPORT_ENABLED", "false")
    monkeypatch.setenv("ASSISTANT_AGENT_LANGFUSE_SCORE_ENABLED", "false")
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
def test_gateway_timeout_preserves_partial_correlation() -> None:
    async def scenario() -> None:
        endpoint = HangingGatewayEndpoint()
        facade = GatewayTurnFacade(manager=GatewayManagerStub(endpoint))
        observed = []
        turn_task = None
        try:
            turn_task = asyncio.create_task(
                facade.run_turn(
                    GatewayTurnRequest(
                        user_id="user-sentinel",
                        session_id="session-sentinel",
                        text="request-sentinel",
                        timeout_s=0.1,
                    ),
                    on_correlation=observed.append,
                )
            )
            await asyncio.wait_for(
                endpoint.correlation_sent.wait(),
                timeout=0.1,
            )
            with pytest.raises(GatewayTurnTimeout) as captured:
                await turn_task
        finally:
            if turn_task is not None and not turn_task.done():
                turn_task.cancel()
                await asyncio.gather(turn_task, return_exceptions=True)
            await facade.close()

        correlation = captured.value.correlation
        assert correlation is not None
        assert correlation.run_id == endpoint.sent[0]["payload"]["run_id"]
        assert correlation.trace_id == "trace-sentinel"
        assert observed[-1] == correlation
        assert endpoint.sent[-1]["type"] == "run.cancel"
        assert endpoint.sent[-1]["payload"]["reason"] == "facade_timeout"

    asyncio.run(scenario())


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


@pytest.mark.core_invariant("OBS-001")
def test_started_events_define_span_start_times() -> None:
    base = datetime(2026, 7, 28, 7, 27, 20, tzinfo=timezone.utc)
    context_started_at = base
    context_finished_at = base + timedelta(microseconds=500)
    llm_started_at = base + timedelta(microseconds=700)
    llm_finished_at = base + timedelta(seconds=2, microseconds=100)
    events = [
        _trace_event(
            canonical_event="context.build.started",
            span_id="context-span",
            created_at=context_started_at,
            status="started",
        ),
        _trace_event(
            canonical_event="context.build.finished",
            span_id="context-span",
            created_at=context_finished_at,
            status="succeeded",
            latency_ms=0,
            observation_type="span",
            observation_name="context.compile",
        ),
        _trace_event(
            canonical_event="llm.chat.started",
            span_id="llm-span",
            created_at=llm_started_at,
            status="started",
        ),
        _trace_event(
            canonical_event="llm.chat.finished",
            span_id="llm-span",
            created_at=llm_finished_at,
            status="succeeded",
            latency_ms=2_000,
            observation_type="generation",
        ),
    ]

    spans = build_text_otel_span_specs(events)
    context_span = next(
        span for span in spans if span.name == "context.compile"
    )
    llm_span = next(span for span in spans if span.name == "llm.chat")

    assert context_span.start_time == context_started_at
    assert context_span.end_time == context_finished_at
    assert llm_span.start_time == llm_started_at
    assert llm_span.end_time == llm_finished_at
    assert context_span.end_time <= llm_span.start_time


@pytest.mark.core_invariant("OBS-001")
def test_context_preflight_metadata_does_not_duplicate_generation_usage() -> None:
    created_at = datetime(2026, 8, 6, 10, 0, tzinfo=timezone.utc)
    events = [
        _trace_event(
            canonical_event="context.build.finished",
            span_id="context-span",
            created_at=created_at,
            status="succeeded",
            observation_type="span",
            observation_name="context.compile",
            attributes={
                "iteration": 1,
                "compiled_input_tokens": 120,
                "effective_input_limit": 1_000,
                "context_token_usage_ratio": 0.12,
                "tokenizer_id": "tokenizer-sentinel",
                "token_accounting_status": "available",
                "total_tokens": 0,
            },
        ),
        _trace_event(
            canonical_event="llm.chat.finished",
            span_id="llm-span",
            created_at=created_at + timedelta(seconds=1),
            status="succeeded",
            observation_type="generation",
            attributes={
                "iteration": 1,
                "usage": {
                    "prompt_tokens": 125,
                    "completion_tokens": 25,
                    "total_tokens": 150,
                },
            },
        ),
    ]

    spans = build_text_otel_span_specs(events)
    context_attributes = next(
        span.attributes for span in spans if span.name == "context.compile"
    )
    llm_attributes = next(
        span.attributes for span in spans if span.name == "llm.chat"
    )

    assert context_attributes[
        "langfuse.observation.metadata.assistant_agent.compiled_input_tokens"
    ] == 120
    assert context_attributes[
        "langfuse.observation.metadata.assistant_agent.effective_input_limit"
    ] == 1_000
    assert context_attributes[
        "langfuse.observation.metadata.assistant_agent.context_token_usage_ratio"
    ] == 0.12
    assert context_attributes[
        "langfuse.observation.metadata.assistant_agent.tokenizer_id"
    ] == "tokenizer-sentinel"
    assert context_attributes[
        "langfuse.observation.metadata.assistant_agent.token_accounting_status"
    ] == "available"
    assert "langfuse.observation.usage_details" not in context_attributes
    assert json.loads(llm_attributes["langfuse.observation.usage_details"]) == {
        "input": 125,
        "output": 25,
        "total": 150,
    }


def _trace_event(
    *,
    canonical_event: str,
    span_id: str,
    created_at: datetime,
    status: str,
    latency_ms: int | None = None,
    observation_type: str | None = None,
    observation_name: str | None = None,
    attributes: dict | None = None,
) -> TraceEvent:
    return TraceEvent(
        trace_id="trace-sentinel",
        run_id="run-sentinel",
        node_name="node-sentinel",
        event_type="observability",
        canonical_event=canonical_event,
        observation_type=observation_type,
        observation_name=observation_name,
        observation_scope="iteration",
        span_id=span_id,
        status=status,
        latency_ms=latency_ms,
        attributes=attributes or {"iteration": 1},
        created_at=created_at,
    )
