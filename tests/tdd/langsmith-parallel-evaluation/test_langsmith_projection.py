from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from assistant_agent.observability.otel_mapping import build_text_otel_span_specs
from assistant_agent.observability.trace_conversation import (
    TraceConversationText,
    TraceConversationView,
)
from assistant_agent.observability.trace_store import TraceEvent


def _event(
    canonical_event: str,
    *,
    offset: int,
    span_id: str,
    parent_span_id: str | None,
    observation_type: str = "span",
    observation_name: str | None = None,
    status: str | None = None,
    tool_name: str | None = None,
    attributes: dict | None = None,
    output_summary: dict | None = None,
) -> TraceEvent:
    return TraceEvent(
        trace_id="a" * 32,
        run_id="run-1",
        user_id="user-1",
        session_id="session-1",
        node_name="runtime",
        event_type="observability",
        canonical_event=canonical_event,
        observation_type=observation_type,
        observation_name=observation_name,
        span_id=span_id,
        parent_span_id=parent_span_id,
        status=status,
        tool_name=tool_name,
        attributes=attributes or {},
        output_summary=output_summary or {},
        created_at=datetime(2026, 8, 11, tzinfo=timezone.utc)
        + timedelta(milliseconds=offset),
    )


def test_runtime_projection_uses_platform_neutral_and_langsmith_semantics() -> None:
    runtime_span_id = "1" * 16
    llm_span_id = "2" * 16
    tool_span_id = "3" * 16
    events = [
        _event(
            "run.started",
            offset=0,
            span_id=runtime_span_id,
            parent_span_id=None,
            status="started",
        ),
        _event(
            "llm.chat.finished",
            offset=1,
            span_id=llm_span_id,
            parent_span_id=runtime_span_id,
            observation_type="generation",
            observation_name="llm.chat",
            status="completed",
        ),
        _event(
            "tool.finished",
            offset=2,
            span_id=tool_span_id,
            parent_span_id=runtime_span_id,
            observation_name="tool.execute",
            status="completed",
            tool_name="weather.lookup",
        ),
        _event(
            "assistant.turn.summary",
            offset=3,
            span_id=runtime_span_id,
            parent_span_id=None,
            status="completed",
            output_summary={
                "run_id": "run-1",
                "trace_id": "a" * 32,
                "user_id": "user-1",
                "session_id": "session-1",
                "terminal_status": "completed",
                "response_present": True,
                "tool_count": 1,
                "error_count": 0,
            },
        ),
    ]
    conversation = TraceConversationView(
        trace_id="a" * 32,
        user=TraceConversationText(text="这个周末天气如何？", chars=9),
        assistant=TraceConversationText(text="周末晴朗。", chars=6),
    )

    spans = build_text_otel_span_specs(events, conversation=conversation)

    root = next(span for span in spans if span.name == "agent.runtime")
    llm = next(span for span in spans if span.name == "llm.chat")
    tool = next(span for span in spans if span.name == "tool.execute")
    assert root.attributes["langsmith.trace.name"] == "assistant.turn"
    assert root.attributes["langsmith.span.kind"] == "chain"
    assert root.attributes["langsmith.trace.session_id"] == "session-1"
    assert root.attributes["langsmith.metadata.run_id"] == "run-1"
    assert json.loads(root.attributes["inputs"])["role"] == "user"
    assert json.loads(root.attributes["outputs"])["role"] == "assistant"
    assert llm.attributes["langsmith.span.kind"] == "llm"
    assert tool.attributes["langsmith.span.kind"] == "tool"
    assert llm.attributes["assistant_agent.observation.type"] == "generation"
    assert tool.attributes["assistant_agent.observation.type"] == "span"
