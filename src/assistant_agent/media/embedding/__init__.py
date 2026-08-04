"""Unified image/text embedding contracts and providers."""

from assistant_agent.media.embedding.comparator import (
    EmbeddingComparator,
    EmbeddingComparisonError,
)
from assistant_agent.media.embedding.models import (
    EmbeddingEvent,
    EmbeddingFailureEvent,
    EmbeddingOutcome,
    EmbeddingReadiness,
    ImageObservation,
    TextObservation,
)
from assistant_agent.media.embedding.provider import (
    MockMultimodalEmbeddingProvider,
    MultimodalEmbeddingProvider,
    create_multimodal_embedding_provider,
)

__all__ = [
    "EmbeddingComparator",
    "EmbeddingComparisonError",
    "EmbeddingEvent",
    "EmbeddingFailureEvent",
    "EmbeddingOutcome",
    "EmbeddingReadiness",
    "ImageObservation",
    "MockMultimodalEmbeddingProvider",
    "MultimodalEmbeddingProvider",
    "TextObservation",
    "create_multimodal_embedding_provider",
]
