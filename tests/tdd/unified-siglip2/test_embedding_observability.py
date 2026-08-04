from assistant_agent.media.embedding.models import (
    EmbeddingEvent,
    ImageObservation,
    TextObservation,
)
from assistant_agent.media.embedding.observability import (
    EMBEDDING_EVENT_NAMES,
    InMemoryEmbeddingObserver,
    embedding_trace_payload,
    emit_embedding_observation,
)


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
