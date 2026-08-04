from __future__ import annotations

import math

import pytest

from assistant_agent.media.embedding.comparator import (
    EmbeddingComparator,
    EmbeddingComparisonError,
)
from assistant_agent.media.embedding.models import (
    EmbeddingEvent,
    EmbeddingFailureEvent,
    ImageObservation,
    TextObservation,
)
from assistant_agent.media.embedding.provider import MockMultimodalEmbeddingProvider


def _event(
    *,
    modality: str,
    vector: list[float],
    space: str = "space-sentinel",
) -> EmbeddingEvent:
    return EmbeddingEvent(
        event_id=f"event-{modality}",
        modality=modality,
        vector=vector,
        embedding_space_id=space,
        model_id="model-sentinel",
        model_revision="a" * 40,
        dimension=len(vector),
        normalized=True,
        session_id="session-sentinel",
        source_observation_id=f"observation-{modality}",
        latency_ms=1,
    )


def test_comparator_scores_compatible_cross_modal_vectors() -> None:
    similarity = EmbeddingComparator().similarity(
        _event(modality="image", vector=[1.0, 0.0]),
        _event(modality="text", vector=[0.6, 0.8]),
    )

    assert similarity == pytest.approx(0.6)


def test_comparator_rejects_different_embedding_spaces() -> None:
    with pytest.raises(EmbeddingComparisonError, match="embedding_space_mismatch"):
        EmbeddingComparator().similarity(
            _event(modality="image", vector=[1.0, 0.0], space="space-a"),
            _event(modality="text", vector=[1.0, 0.0], space="space-b"),
        )


def test_comparator_rejects_dimension_and_normalization_mismatches() -> None:
    with pytest.raises(EmbeddingComparisonError, match="embedding_dimension_mismatch"):
        EmbeddingComparator().similarity(
            _event(modality="image", vector=[1.0, 0.0]),
            _event(modality="text", vector=[1.0, 0.0, 0.0]),
        )

    unnormalized = _event(modality="text", vector=[1.0, 0.0]).model_copy(
        update={"normalized": False}
    )
    with pytest.raises(EmbeddingComparisonError, match="embedding_not_normalized"):
        EmbeddingComparator().similarity(
            _event(modality="image", vector=[1.0, 0.0]),
            unnormalized,
        )


def test_comparator_rejects_non_finite_values() -> None:
    bad = _event(modality="text", vector=[math.nan, 0.0])

    with pytest.raises(EmbeddingComparisonError, match="embedding_non_finite"):
        EmbeddingComparator().similarity(
            _event(modality="image", vector=[1.0, 0.0]),
            bad,
        )


def test_failure_event_cannot_carry_a_vector() -> None:
    assert "vector" not in EmbeddingFailureEvent.model_fields
    failure = EmbeddingFailureEvent(
        modality="text",
        session_id="session-sentinel",
        source_observation_id="observation-text",
        code="embedding_unavailable",
        safe_message="embedding unavailable",
        recoverable=True,
        latency_ms=2,
    )

    assert failure.code == "embedding_unavailable"


def test_mock_provider_embeds_both_modalities_without_external_io() -> None:
    provider = MockMultimodalEmbeddingProvider(dimension=4)
    image = provider.embed_image(
        ImageObservation(
            session_id="session-sentinel",
            observation_id="frame-1",
            image_ref="/tmp/frame-sentinel.jpg",
            video_id="video-sentinel",
            frame_sequence=1,
        )
    )
    text = provider.embed_text(
        TextObservation(
            session_id="session-sentinel",
            observation_id="text-1",
            text="钥匙",
            source="user_request",
        )
    )

    assert isinstance(image, EmbeddingEvent)
    assert isinstance(text, EmbeddingEvent)
    assert image.embedding_space_id == text.embedding_space_id
    assert image.dimension == text.dimension == 4
    assert sum(value * value for value in image.vector) == pytest.approx(1.0)
    assert sum(value * value for value in text.vector) == pytest.approx(1.0)
    assert provider.readiness().image_ready is True
    assert provider.readiness().text_ready is True
