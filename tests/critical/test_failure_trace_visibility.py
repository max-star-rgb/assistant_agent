"""Regression coverage for partial-trace visibility after entry timeouts."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from assistant_agent.realtime.event_mapping import map_agent_progress_event
from assistant_agent.schemas.events import AgentEvent
from assistant_agent.services.agent_service_delivery import AgentServiceDeliveryRegistry
from assistant_agent.services.agent_service_latency import (
    AgentServiceTurnTiming,
    analyze_agent_service_turn,
    append_turn_latency_trace,
)
from assistant_agent.services.gateway_turn_facade import (
    GatewayTurnFacade,
    GatewayTurnRequest,
    GatewayTurnTimeout,
)
from assistant_agent.services.trace_store import InMemoryTraceStore, TraceEvent
from assistant_agent.services.turn_evaluator import build_turn_diagnostic
from assistant_agent.services.turn_summary import append_agent_service_turn_summary
from scripts import agentruntime_view


class _JsonResponse:
    def __init__(self, payload: dict) -> None:
        self.body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def test_langfuse_trace_fallback_restores_persisted_conversation(monkeypatch) -> None:
    captured_authorization: list[str] = []

    def fake_urlopen(request, timeout):
        assert timeout == 10
        captured_authorization.append(request.get_header("Authorization"))
        return _JsonResponse(
            {
                "input": {"role": "user", "content": "用户原文"},
                "output": {"role": "assistant", "content": "最终回答"},
                "observations": [
                    {
                        "name": "llm.chat",
                        "input": {"messages": [{"role": "user", "content": "用户原文"}]},
                        "output": {"normalized_result": {"response_text": "最终回答"}},
                    }
                ],
            }
        )

    monkeypatch.setattr(agentruntime_view, "urlopen", fake_urlopen)
    trace = agentruntime_view._get_langfuse_trace(
        "trace-persisted",
        env={
            "LANGFUSE_HOST": "http://localhost:3000",
            "LANGFUSE_PUBLIC_KEY": "pk-local",
            "LANGFUSE_SECRET_KEY": "sk-local",
        },
    )

    assert trace is not None
    conversation = agentruntime_view._conversation_from_langfuse_trace(
        "trace-persisted", trace
    )
    assert conversation["source"] == "langfuse_public_api"
    assert conversation["user"]["text"] == "用户原文"
    assert conversation["assistant"]["text"] == "最终回答"
    assert conversation["llm_outputs"][0]["normalized_result"]["response_text"] == "最终回答"
    assert captured_authorization == ["Basic cGstbG9jYWw6c2stbG9jYWw="]


class _HangingGatewayEndpoint:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._received: asyncio.Queue[dict] = asyncio.Queue()

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._received.get()

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
        await self._received.put({"type": "run.started", **common})
        await self._received.put(
            {
                "type": "event.progress",
                **common,
                "payload": {
                    "agent_event_type": "task_started",
                    "assistant_run_id": "assistant-run-1",
                    "trace_id": "trace-1",
                },
            }
        )


class _GatewayManagerStub:
    def __init__(self, endpoint: _HangingGatewayEndpoint) -> None:
        self.endpoint = endpoint

    async def acquire(self, **_kwargs):
        return SimpleNamespace(endpoint=self.endpoint)


class _DeliveryAudit:
    def __init__(self) -> None:
        self.records: list[tuple[object, str, dict]] = []

    def append(self, delivery, event_type: str, **metadata) -> None:
        self.records.append((delivery, event_type, metadata))


def test_runtime_announces_trace_correlation_before_work() -> None:
    mapped = map_agent_progress_event(
        AgentEvent(
            type="task_started",
            session_id="session-1",
            run_id="assistant-run-1",
            payload={
                "assistant_run_id": "assistant-run-1",
                "trace_id": "trace-1",
            },
        )
    )

    assert mapped is not None
    assert mapped.type == "run.progress"
    assert mapped.payload["assistant_run_id"] == "assistant-run-1"
    assert mapped.payload["trace_id"] == "trace-1"


def test_gateway_timeout_preserves_partial_trace_correlation() -> None:
    asyncio.run(_assert_gateway_timeout_preserves_partial_trace_correlation())


async def _assert_gateway_timeout_preserves_partial_trace_correlation() -> None:
    endpoint = _HangingGatewayEndpoint()
    facade = GatewayTurnFacade(manager=_GatewayManagerStub(endpoint))
    observed = []
    try:
        with pytest.raises(GatewayTurnTimeout) as captured:
            await facade.run_turn(
                GatewayTurnRequest(
                    user_id="user-1",
                    session_id="session-1",
                    text="hello",
                    timeout_s=0.01,
                ),
                on_correlation=observed.append,
            )
    finally:
        await facade.close()

    correlation = captured.value.correlation
    assert correlation is not None
    assert correlation.assistant_run_id == "assistant-run-1"
    assert correlation.trace_id == "trace-1"
    assert observed[-1] == correlation
    cancel = endpoint.sent[-1]
    assert cancel["type"] == "run.cancel"
    assert cancel["payload"]["reason"] == "facade_timeout"


def test_timeout_failure_audit_keeps_all_correlation_ids() -> None:
    audit = _DeliveryAudit()
    registry = AgentServiceDeliveryRegistry(audit_sink=audit)
    delivery = registry.accept("session-1", "chat-1", expects_ack=False)

    failed = registry.mark_failed(
        delivery.delivery_id,
        error_code="gateway_turn_timeout",
        gateway_run_id="gateway-run-1",
        assistant_run_id="assistant-run-1",
        trace_id="trace-1",
        runtime_status="pending_cancel",
        failure_source="gateway_turn_facade",
    )

    assert failed.gateway_run_id == "gateway-run-1"
    assert failed.assistant_run_id == "assistant-run-1"
    assert failed.trace_id == "trace-1"
    _, event_type, metadata = audit.records[-1]
    assert event_type == "failed"
    assert metadata == {
        "error_code": "gateway_turn_timeout",
        "runtime_status": "pending_cancel",
        "failure_source": "gateway_turn_facade",
    }


def test_partial_trace_reports_layered_timeout_and_open_stage() -> None:
    timing = AgentServiceTurnTiming(
        delivery_id="delivery-1",
        session_turn=1,
        chat_index_digest="chat-digest",
        expects_ack=False,
        received_ns=1_000_000,
        accepted_ns=2_000_000,
        user_id="user-1",
        session_id="agent-service-session-1",
    )
    timing.bind_turn(
        turn_id="turn-1",
        gateway_run_id="gateway-run-1",
        assistant_run_id="assistant-run-1",
        trace_id="trace-1",
    )
    timing.mark("failed", at_ns=32_000_000)
    timing.mark("send_finished", at_ns=33_000_000)
    timing.mark_failure(
        code="gateway_turn_timeout",
        source="gateway_turn_facade",
        runtime_status="pending_cancel",
        deadline_ms=30_000,
    )
    events = [
        TraceEvent(
            trace_id="trace-1",
            run_id="assistant-run-1",
            user_id="user-1",
            session_id="agent-service-session-1",
            node_name="assistant_loop",
            event_type="observability",
            canonical_event="llm.chat.started",
            span_id="span-llm-2",
            status="started",
            attributes={"iteration": 2},
        )
    ]
    summary = analyze_agent_service_turn(timing, events, status="failed")
    store = InMemoryTraceStore()
    store.events.extend(events)

    assert append_turn_latency_trace(store, timing=timing, summary=summary)
    assert append_agent_service_turn_summary(
        store,
        timing=timing,
        latency_summary=summary,
        events=events,
    )

    assert summary.runtime_status == "pending_cancel"
    assert summary.active_stage == "llm_chat[2]"
    assert summary.open_span_count == 1
    assert agentruntime_view._follow_update_ready(store.events)
    payload = agentruntime_view._summary_payload(store.events)
    diagnostic = build_turn_diagnostic(store.events, payload=payload)
    rendered = agentruntime_view._format_human(payload, show_errors=True, sections=("overview", "timeline"))
    assert diagnostic.execution_status == "pending_cancel"
    assert diagnostic.delivery_status == "failed"
    assert diagnostic.task_outcome == "unknown"
    assert diagnostic.text_ux_status == "failed"
    assert "gateway_turn_timeout" in rendered
    assert "active_stage=llm_chat[2]" in rendered
    assert not any(event.canonical_event == "llm.chat.finished" for event in store.events)
    assert not any(event.canonical_event == "run.failed" for event in store.events)
