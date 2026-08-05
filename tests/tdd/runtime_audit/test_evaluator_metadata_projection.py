from __future__ import annotations

from datetime import datetime, timezone

from assistant_agent.memory.trace_content import MemoryIngestionTraceContent
from assistant_agent.observability.otel_mapping import (
    build_late_text_otel_span_spec,
    build_text_otel_span_specs,
)
from assistant_agent.observability.trace_store import TraceEvent


UTC = timezone.utc


def test_final_llm_action_is_filterable_langfuse_observation_metadata() -> None:
    """Would fail if live response evaluators could not select the final generation."""

    event = TraceEvent(
        trace_id="trace-sentinel",
        run_id="run-sentinel",
        node_name="assistant_loop",
        event_type="observability",
        canonical_event="llm.chat.finished",
        observation_type="generation",
        observation_scope="iteration",
        span_id="llm-span-sentinel",
        status="succeeded",
        attributes={"iteration": 1, "runtime_action": "text"},
        created_at=datetime(2026, 8, 5, 7, 2, tzinfo=UTC),
    )

    llm_span = next(
        span for span in build_text_otel_span_specs([event]) if span.name == "llm.chat"
    )

    assert llm_span.attributes["assistant_agent.runtime_action"] == "text"
    assert (
        llm_span.attributes[
            "langfuse.observation.metadata.assistant_agent.runtime_action"
        ]
        == "text"
    )


def test_memory_evidence_is_filterable_langfuse_observation_metadata() -> None:
    """Would fail if live memory extraction evaluators could not select evidence spans."""

    event = TraceEvent(
        trace_id="trace-sentinel",
        run_id="run-sentinel",
        user_id="user-sentinel",
        session_id="session-sentinel",
        node_name="post_response_memory_ingestion",
        event_type="observability",
        canonical_event="memory.ingestion.finished",
        observation_type="span",
        observation_name="memory.turn_ingestion",
        span_id="memory-span-sentinel",
        status="succeeded",
        created_at=datetime(2026, 8, 5, 7, 2, 2, tzinfo=UTC),
    )
    memory_content = MemoryIngestionTraceContent(
        trace_id="trace-sentinel",
        run_id="run-sentinel",
        user_id="user-sentinel",
        session_id="session-sentinel",
        source_turn="turn-sentinel",
        user_text="user-sentinel-text",
        assistant_text="assistant-sentinel-text",
    )

    memory_span = build_late_text_otel_span_spec(
        event,
        memory_content=memory_content,
    )

    assert (
        memory_span.attributes["assistant_agent.memory_semantic_evidence"]
        == "available"
    )
    assert (
        memory_span.attributes[
            "langfuse.observation.metadata.assistant_agent.memory_semantic_evidence"
        ]
        == "available"
    )
