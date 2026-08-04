from assistant_agent.media.embedding.consumers.alignment import CrossModalAlignmentConsumer
from assistant_agent.media.embedding.models import EmbeddingEvent


def _event(event_id: str, modality: str, vector, at_ms: int, *, space: str = "joint"):
    kwargs = {"captured_at_ms": at_ms} if modality == "image" else {"occurred_at_ms": at_ms}
    return EmbeddingEvent(
        event_id=event_id,
        modality=modality,
        vector=vector,
        embedding_space_id=space,
        model_id="model",
        model_revision="revision",
        dimension=2,
        normalized=True,
        session_id="session-1",
        source_observation_id=event_id,
        latency_ms=0,
        **kwargs,
    )


def test_alignment_orders_by_similarity_then_temporal_distance() -> None:
    consumer = CrossModalAlignmentConsumer()
    text = _event("query", "text", [1.0, 0.0], 1000)
    near = _event("near-frame", "image", [1.0, 0.0], 900)
    far = _event("far-frame", "image", [1.0, 0.0], 9000)

    result = consumer.align(text, [far, near])

    assert [item.image_observation_id for item in result] == ["near-frame", "far-frame"]
    assert result[0].temporal_distance_ms == 100


def test_alignment_rejects_mismatched_spaces() -> None:
    consumer = CrossModalAlignmentConsumer()

    result = consumer.align(
        _event("query", "text", [1.0, 0.0], 1000, space="a"),
        [_event("frame", "image", [1.0, 0.0], 900, space="b")],
    )

    assert result == []


def test_alignment_accept_tracks_only_bounded_success_events() -> None:
    consumer = CrossModalAlignmentConsumer(max_events=2)
    for index in range(3):
        event = _event(f"frame-{index}", "image", [1.0, 0.0], index)
        consumer.accept(event, object())

    assert [item.source_observation_id for item in consumer.image_events()] == [
        "frame-1",
        "frame-2",
    ]
