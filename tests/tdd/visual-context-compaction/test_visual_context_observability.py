from __future__ import annotations

from assistant_agent.media.embedding.observability import (
    InMemoryEmbeddingObserver,
    emit_visual_context_observation,
)


def test_visual_context_events_only_expose_budget_facts() -> None:
    observer = InMemoryEmbeddingObserver()

    emit_visual_context_observation(
        observer,
        "visual_context.compacted",
        session_id="secret-session",
        sequence=9,
        input_tokens=90,
        effective_input_limit=80,
        target_tokens=32,
        usage_ratio=1.125,
        output_tokens=30,
        covered_count=4,
        recent_count=2,
        revision=3,
        latency_ms=12,
        status="succeeded",
        compacted=False,
        text="secret-text",
        summary="secret-summary",
        query="secret-query",
        record_ids=["secret-record"],
        path="/secret/frame.jpg",
        vector=[1.0, 0.0],
        raw_response={"secret": True},
    )

    event = observer.events[-1]
    payload = event.payload
    assert event.event_name == "visual_context.compacted"
    assert payload == {
        "session_id_digest": payload["session_id_digest"],
        "sequence": 9,
        "input_tokens": 90,
        "effective_input_limit": 80,
        "target_tokens": 32,
        "usage_ratio": 1.125,
        "covered_count": 4,
        "recent_count": 2,
        "revision": 3,
        "latency_ms": 12,
        "status": "succeeded",
        "compacted": False,
    }
    assert payload["session_id_digest"] != "secret-session"
