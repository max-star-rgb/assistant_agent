from assistant_agent.media.embedding.consumers.attention import VisualAttentionConsumer

from test_cross_modal_alignment import _event


def test_attention_only_emits_internal_candidate() -> None:
    consumer = VisualAttentionConsumer(similarity_threshold=0.8)
    consumer.set_internal_target(_event("target", "text", [1.0, 0.0], 1000))

    candidate = consumer.observe(_event("frame", "image", [0.9, 0.1], 1100))

    assert candidate is not None
    assert candidate.kind == "visual_attention_candidate"
    assert not hasattr(consumer, "send_message")
    assert not hasattr(consumer, "create_task")


def test_attention_requires_explicit_internal_target_and_threshold() -> None:
    consumer = VisualAttentionConsumer(similarity_threshold=0.95)

    assert consumer.observe(_event("frame", "image", [1.0, 0.0], 1100)) is None
    consumer.set_internal_target(_event("target", "text", [1.0, 0.0], 1000))
    assert consumer.observe(_event("weak", "image", [0.0, 1.0], 1100)) is None
    assert consumer.candidate_events() == []
