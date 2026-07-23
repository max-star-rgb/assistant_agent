"""Regression coverage for Langfuse generation token usage mapping."""

import json
from datetime import datetime, timezone

from assistant_agent.services.otel_mapping import build_text_otel_span_specs
from assistant_agent.services.trace_store import TraceEvent


def test_nested_normalized_provider_usage_reaches_langfuse_generation() -> None:
    event = TraceEvent(
        trace_id="1234567890abcdef1234567890abcdef",
        run_id="run-token-usage",
        user_id="user-token-usage",
        session_id="session-token-usage",
        node_name="assistant",
        event_type="observability",
        canonical_event="llm.chat.finished",
        observation_type="generation",
        observation_scope="iteration",
        status="succeeded",
        provider="qwen",
        model="qwen3.6-flash",
        span_id="0123456789abcdef",
        latency_ms=100,
        attributes={
            "iteration": 1,
            "usage": {
                "prompt_tokens": 3913,
                "completion_tokens": 66,
                "total_tokens": 3979,
            },
        },
        created_at=datetime.now(timezone.utc),
    )

    generation = next(
        span for span in build_text_otel_span_specs([event]) if span.name == "llm.chat"
    )
    usage = json.loads(generation.attributes["langfuse.observation.usage_details"])

    assert usage == {"input": 3913, "output": 66, "total": 3979}
    assert generation.attributes["gen_ai.usage.input_tokens"] == 3913
    assert generation.attributes["gen_ai.usage.output_tokens"] == 66
