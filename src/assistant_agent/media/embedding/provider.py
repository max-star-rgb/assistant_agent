"""Unified image/text embedding Provider boundary."""

from __future__ import annotations

import hashlib
from math import sqrt
from pathlib import Path
from time import perf_counter
from typing import Protocol

from assistant_agent.media.embedding.models import (
    EmbeddingEvent,
    EmbeddingFailureEvent,
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


class UnavailableMultimodalEmbeddingProvider:
    """Fail-closed boundary used when real joint embeddings are unconfigured."""

    provider = "unavailable"
    model_id = "unavailable-multimodal-embedding"

    def embed_image(self, observation: ImageObservation) -> EmbeddingOutcome:
        return self._failure("image", observation)

    def embed_text(self, observation: TextObservation) -> EmbeddingOutcome:
        return self._failure("text", observation)

    def readiness(self) -> EmbeddingReadiness:
        return EmbeddingReadiness(
            provider=self.provider,
            model_id=self.model_id,
            issues=["provider_unconfigured"],
        )

    def _failure(
        self,
        modality: str,
        observation: ImageObservation | TextObservation,
    ) -> EmbeddingFailureEvent:
        return EmbeddingFailureEvent(
            modality=modality,
            session_id=observation.session_id,
            source_observation_id=observation.observation_id,
            code="provider_unconfigured",
            safe_message="real multimodal embedding provider is not configured",
            recoverable=False,
            latency_ms=0,
            model_id=self.model_id,
        )

class DashScopeImageOnlyEmbeddingProvider:
    """Compatibility adapter: DashScope has no proven matching text space here."""

    provider = "dashscope"

    def __init__(self, delegate) -> None:
        self._delegate = delegate
        self.model_id = delegate.config.model
        self.dimension = delegate.config.dimension
        self.embedding_space_id = f"dashscope:{self.model_id}:image-only"

    def embed_image(self, observation: ImageObservation) -> EmbeddingOutcome:
        from assistant_agent.media.video.types import VideoFrame

        started = perf_counter()
        result = self._delegate.embed_image(
            VideoFrame(
                frame_id=observation.observation_id,
                timestamp_seconds=(observation.captured_at_ms or 0) / 1000,
                uri=observation.image_ref,
            )
        )
        if result.errors or not result.embedding:
            error = result.errors[0] if result.errors else {}
            return EmbeddingFailureEvent(
                modality="image",
                session_id=observation.session_id,
                source_observation_id=observation.observation_id,
                code=str(error.get("code") or "provider_empty_response"),
                safe_message=str(error.get("message") or "DashScope image embedding failed"),
                recoverable=bool(error.get("recoverable", True)),
                latency_ms=max(0, int((perf_counter() - started) * 1000)),
                model_id=self.model_id,
                model_revision="provider-managed",
                embedding_space_id=self.embedding_space_id,
            )
        vector = _normalize(result.embedding)
        event_id = hashlib.sha256(
            f"{observation.session_id}:{observation.observation_id}:image".encode()
        ).hexdigest()[:24]
        return EmbeddingEvent(
            event_id=f"embedding-{event_id}",
            modality="image",
            vector=vector,
            embedding_space_id=self.embedding_space_id,
            model_id=self.model_id,
            model_revision="provider-managed",
            dimension=len(vector),
            normalized=True,
            session_id=observation.session_id,
            source_observation_id=observation.observation_id,
            video_id=observation.video_id,
            frame_sequence=observation.frame_sequence,
            captured_at_ms=observation.captured_at_ms,
            latency_ms=max(0, int((perf_counter() - started) * 1000)),
        )

    def embed_text(self, observation: TextObservation) -> EmbeddingOutcome:
        return EmbeddingFailureEvent(
            modality="text",
            session_id=observation.session_id,
            source_observation_id=observation.observation_id,
            code="modality_unavailable",
            safe_message="DashScope text embedding is not enabled for this image space",
            recoverable=False,
            latency_ms=0,
            model_id=self.model_id,
            model_revision="provider-managed",
            embedding_space_id=self.embedding_space_id,
        )

    def readiness(self) -> EmbeddingReadiness:
        configured = bool(self._delegate.config.api_key)
        return EmbeddingReadiness(
            provider=self.provider,
            model_id=self.model_id,
            model_revision="provider-managed",
            embedding_space_id=self.embedding_space_id,
            dimension=self.dimension,
            image_ready=configured,
            text_ready=False,
            issues=[] if configured else ["provider_unconfigured"],
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
        from assistant_agent.media.embedding.local_siglip2 import (
            LocalSiglip2EmbeddingConfig,
            LocalSiglip2EmbeddingProvider,
        )

        model_dir = getattr(config, "siglip2_model_dir", None)
        return LocalSiglip2EmbeddingProvider(
            LocalSiglip2EmbeddingConfig(
                model_dir=Path(model_dir) if model_dir else None,
                cuda_device_id=getattr(config, "embedding_cuda_device_id", 0),
            )
        )
    if provider == "dashscope":
        from assistant_agent.media.video.detection.vision_embedding_provider import (
            DashScopeVisionEmbeddingConfig,
            DashScopeVisionEmbeddingProvider,
        )

        return DashScopeImageOnlyEmbeddingProvider(
            DashScopeVisionEmbeddingProvider(
                DashScopeVisionEmbeddingConfig(
                    api_key=config.vision_embedding_api_key,
                    base_url=config.vision_embedding_base_url,
                    model=config.vision_embedding_model,
                    dimension=config.vision_embedding_dimension,
                    timeout_seconds=config.vision_embedding_timeout_seconds,
                )
            )
        )
    return UnavailableMultimodalEmbeddingProvider()


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


def _normalize(values: list[float]) -> list[float]:
    norm = sqrt(sum(float(value) ** 2 for value in values))
    if norm <= 0:
        raise ValueError("embedding vector norm must be positive")
    return [float(value) / norm for value in values]
