"""Unified image/text embedding Provider boundary."""

from __future__ import annotations

import hashlib
from math import sqrt
from time import perf_counter
from typing import Protocol

from assistant_agent.media.embedding.models import (
    EmbeddingEvent,
    EmbeddingOutcome,
    EmbeddingReadiness,
    ImageObservation,
    TextObservation,
)


class MultimodalEmbeddingProvider(Protocol):
    """Logical stateless Provider for one compatible image/text space."""

    def embed_image(self, observation: ImageObservation) -> EmbeddingOutcome:
        """Embed one image observation."""

    def embed_text(self, observation: TextObservation) -> EmbeddingOutcome:
        """Embed one text observation."""

    def readiness(self) -> EmbeddingReadiness:
        """Report per-modality readiness without running inference."""


class MockMultimodalEmbeddingProvider:
    """Deterministic offline Provider for tests and mock mode."""

    provider = "mock"
    model_id = "mock-multimodal-embedding"
    model_revision = "mock-v1"
    embedding_space_id = "mock-multimodal-space-v1"

    def __init__(self, *, dimension: int = 8) -> None:
        if dimension <= 0:
            raise ValueError("embedding dimension must be positive")
        self.dimension = dimension

    def embed_image(self, observation: ImageObservation) -> EmbeddingOutcome:
        return self._event(
            modality="image",
            observation_id=observation.observation_id,
            session_id=observation.session_id,
            seed=f"image:{observation.observation_id}",
            video_id=observation.video_id,
            frame_sequence=observation.frame_sequence,
            captured_at_ms=observation.captured_at_ms,
        )

    def embed_text(self, observation: TextObservation) -> EmbeddingOutcome:
        return self._event(
            modality="text",
            observation_id=observation.observation_id,
            session_id=observation.session_id,
            seed=f"text:{observation.text}",
            text_source=observation.source,
            occurred_at_ms=observation.occurred_at_ms,
        )

    def readiness(self) -> EmbeddingReadiness:
        return EmbeddingReadiness(
            provider=self.provider,
            model_id=self.model_id,
            model_revision=self.model_revision,
            embedding_space_id=self.embedding_space_id,
            dimension=self.dimension,
            image_ready=True,
            text_ready=True,
        )

    def _event(
        self,
        *,
        modality: str,
        observation_id: str,
        session_id: str,
        seed: str,
        video_id: str | None = None,
        frame_sequence: int | None = None,
        captured_at_ms: int | None = None,
        text_source: str | None = None,
        occurred_at_ms: int | None = None,
    ) -> EmbeddingEvent:
        started_at = perf_counter()
        vector = _deterministic_unit_vector(seed, self.dimension)
        event_digest = hashlib.sha256(
            f"{session_id}:{observation_id}:{modality}".encode("utf-8")
        ).hexdigest()[:24]
        return EmbeddingEvent(
            event_id=f"embedding-{event_digest}",
            modality=modality,
            vector=vector,
            embedding_space_id=self.embedding_space_id,
            model_id=self.model_id,
            model_revision=self.model_revision,
            dimension=self.dimension,
            normalized=True,
            session_id=session_id,
            source_observation_id=observation_id,
            video_id=video_id,
            frame_sequence=frame_sequence,
            captured_at_ms=captured_at_ms,
            text_source=text_source,
            occurred_at_ms=occurred_at_ms,
            latency_ms=max(0, int((perf_counter() - started_at) * 1000)),
        )


def create_multimodal_embedding_provider(config=None) -> MultimodalEmbeddingProvider:
    """Create the configured Provider while keeping mock mode offline."""

    if config is None:
        from assistant_agent.config import ProviderConfig

        config = ProviderConfig.from_env()
    if getattr(config, "provider_mode", "mock") != "real":
        return MockMultimodalEmbeddingProvider()
    provider = getattr(config, "embedding_provider", "mock")
    if provider == "local_siglip2":
        raise RuntimeError("local_siglip2_joint_provider_not_implemented")
    if provider == "dashscope":
        raise RuntimeError("dashscope_unified_provider_not_implemented")
    return MockMultimodalEmbeddingProvider()


def _deterministic_unit_vector(seed: str, dimension: int) -> list[float]:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    values = [
        (digest[index % len(digest)] - 127.5) / 127.5
        for index in range(dimension)
    ]
    norm = sqrt(sum(value * value for value in values))
    if norm == 0.0:
        values[0] = 1.0
        norm = 1.0
    return [value / norm for value in values]
