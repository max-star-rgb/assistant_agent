"""Unified image/text embedding contracts and providers."""

from assistant_agent.media.embedding.comparator import (
    EmbeddingComparator,
    EmbeddingComparisonError,
)
from assistant_agent.media.embedding.coordinator import SessionEmbeddingCoordinator
from assistant_agent.media.embedding.coordinator_store import (
    SessionEmbeddingCoordinatorStore,
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
    DashScopeImageOnlyEmbeddingProvider,
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
    "DashScopeImageOnlyEmbeddingProvider",
    "MultimodalEmbeddingProvider",
    "SessionEmbeddingCoordinator",
    "SessionEmbeddingCoordinatorStore",
    "TextObservation",
    "create_multimodal_embedding_provider",
]
