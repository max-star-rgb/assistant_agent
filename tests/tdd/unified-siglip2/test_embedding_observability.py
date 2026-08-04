import logging

from assistant_agent.config import ProviderConfig
from assistant_agent.media.embedding.models import (
    EmbeddingEvent,
    ImageObservation,
    TextObservation,
)
from assistant_agent.media.embedding.observability import (
    EMBEDDING_EVENT_NAMES,
    SEMANTIC_FRAME_EVENT_NAMES,
    VISUAL_SEMANTIC_EVENT_NAMES,
    InMemoryEmbeddingObserver,
    LoggingEmbeddingObserver,
    embedding_trace_payload,
    emit_embedding_observation,
    emit_semantic_frame_observation,
    emit_visual_semantic_observation,
)
from assistant_agent.runtime.runtime import AgentGraphRuntime


def _event() -> EmbeddingEvent:
    return EmbeddingEvent(
        event_id="event-secret",
        modality="text",
        vector=[0.1, 0.2],
        embedding_space_id="secret-space",
        model_id="model",
        model_revision="revision",
        dimension=2,
        normalized=True,
        session_id="session-secret",
        source_observation_id="observation-secret",
        text_source="user_request",
        occurred_at_ms=1,
        latency_ms=2,
    )


def test_embedding_trace_excludes_vector_text_and_paths() -> None:
    payload = embedding_trace_payload(
        outcome=_event(),
        observation=TextObservation(
            session_id="session-secret",
            observation_id="observation-secret",
            text="very secret text",
            source="user_request",
        ),
    )
    serialized = str(payload)

    assert "vector" not in payload
    assert "text" not in payload
    assert "image_ref" not in payload
    assert "very secret text" not in serialized
    assert "session-secret" not in serialized
    assert "secret-space" not in serialized
    assert payload["embedding_space_id_digest"]


def test_image_path_is_never_observed() -> None:
    payload = embedding_trace_payload(
        observation=ImageObservation(
            session_id="session-secret",
            observation_id="image-secret",
            image_ref="/private/secret/frame.jpg",
        )
    )

    assert "/private/secret/frame.jpg" not in str(payload)


def test_observer_accepts_only_stable_embedding_event_names() -> None:
    observer = InMemoryEmbeddingObserver()

    for name in EMBEDDING_EVENT_NAMES:
        emit_embedding_observation(observer, name, outcome=_event())

    assert [item.event_name for item in observer.events] == list(EMBEDDING_EVENT_NAMES)


def test_semantic_frame_trace_excludes_content_and_raw_identities() -> None:
    observer = InMemoryEmbeddingObserver()

    for name in SEMANTIC_FRAME_EVENT_NAMES:
        emit_semantic_frame_observation(
            observer,
            name,
            session_id="session-secret",
            sequence=7,
            reason="reason-secret",
            replaced_sequence=6,
            image_ref="/private/secret/frame.jpg",
            text="very secret text",
            vector=[0.1, 0.2],
        )

    serialized = str([event.model_dump() for event in observer.events])
    assert [item.event_name for item in observer.events] == list(
        SEMANTIC_FRAME_EVENT_NAMES
    )
    assert all(item.payload["sequence"] == 7 for item in observer.events)
    assert "session-secret" not in serialized
    assert "/private/secret/frame.jpg" not in serialized
    assert "very secret text" not in serialized
    assert "vector" not in serialized
    assert "reason-secret" not in serialized


def test_runtime_coordinator_uses_content_safe_production_observer(caplog) -> None:
    runtime = AgentGraphRuntime(config=ProviderConfig(provider_mode="mock"))
    try:
        coordinator = runtime.embedding_coordinator_store.resolve(
            "user-1",
            "session-1",
        )
        assert isinstance(coordinator.observer, LoggingEmbeddingObserver)

        with caplog.at_level(
            logging.INFO,
            logger="assistant_agent.media.embedding.observability",
        ):
            emit_semantic_frame_observation(
                coordinator.observer,
                "semantic_frame.admitted",
                session_id="session-secret",
                sequence=1,
                image_ref="/private/frame.jpg",
            )

        serialized = caplog.text
        assert "semantic_frame.admitted" in serialized
        assert "session-secret" not in serialized
        assert "/private/frame.jpg" not in serialized
    finally:
        runtime.close()


def test_visual_record_and_query_events_exclude_content() -> None:
    observer = InMemoryEmbeddingObserver()

    for name in VISUAL_SEMANTIC_EVENT_NAMES:
        emit_visual_semantic_observation(
            observer,
            name,
            session_id="session-secret",
            sequence=4,
            status="confirmed",
            count=2,
            latency_ms=3,
            query="secret query",
            evidence_ref="/private/frame.jpg",
            summary="secret summary",
            vector=[1.0, 0.0],
        )

    serialized = str([event.model_dump() for event in observer.events])
    assert [event.event_name for event in observer.events] == list(
        VISUAL_SEMANTIC_EVENT_NAMES
    )
    assert "session-secret" not in serialized
    assert "secret query" not in serialized
    assert "/private/frame.jpg" not in serialized
    assert "secret summary" not in serialized
    assert "vector" not in serialized
