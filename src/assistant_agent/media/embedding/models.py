"""Provider-neutral contracts for unified image/text embedding."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


EmbeddingModality = Literal["image", "text"]
EmbeddingPriority = Literal["interactive", "background"]


class ImageObservation(BaseModel):
    """One image input with trusted session and temporal identity."""

    model_config = ConfigDict(frozen=True)

    session_id: str = Field(min_length=1)
    observation_id: str = Field(min_length=1)
    image_ref: str = Field(min_length=1, exclude=True)
    video_id: str | None = None
    connection_generation: int | None = Field(default=None, ge=1)
    frame_sequence: int | None = Field(default=None, ge=0)
    captured_at_ms: int | None = Field(default=None, ge=0)


class TextObservation(BaseModel):
    """One stable text input; ASR is only one possible upstream source."""

    model_config = ConfigDict(frozen=True)

    session_id: str = Field(min_length=1)
    observation_id: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=4_000, exclude=True)
    source: str = Field(min_length=1, max_length=80)
    occurred_at_ms: int | None = Field(default=None, ge=0)
    final: bool = True


class EmbeddingEvent(BaseModel):
    """Immutable successful embedding result safe for in-process consumers."""

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(min_length=1)
    modality: EmbeddingModality
    vector: list[float] = Field(min_length=1, exclude=True)
    embedding_space_id: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    model_revision: str = Field(min_length=1)
    dimension: int = Field(gt=0)
    normalized: bool
    session_id: str = Field(min_length=1)
    source_observation_id: str = Field(min_length=1)
    video_id: str | None = None
    frame_sequence: int | None = Field(default=None, ge=0)
    captured_at_ms: int | None = Field(default=None, ge=0)
    text_source: str | None = None
    occurred_at_ms: int | None = Field(default=None, ge=0)
    latency_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_vector_dimension(self) -> "EmbeddingEvent":
        if len(self.vector) != self.dimension:
            raise ValueError("embedding vector dimension does not match dimension")
        return self


class EmbeddingFailureEvent(BaseModel):
    """Structured embedding failure that can never masquerade as a vector."""

    model_config = ConfigDict(frozen=True)

    modality: EmbeddingModality
    session_id: str = Field(min_length=1)
    source_observation_id: str = Field(min_length=1)
    code: str = Field(min_length=1, max_length=80)
    safe_message: str = Field(min_length=1, max_length=240)
    recoverable: bool
    latency_ms: int = Field(ge=0)
    model_id: str | None = None
    model_revision: str | None = None
    embedding_space_id: str | None = None


EmbeddingOutcome = EmbeddingEvent | EmbeddingFailureEvent


class EmbeddingReadiness(BaseModel):
    """Per-modality readiness without triggering inference."""

    model_config = ConfigDict(frozen=True)

    provider: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    model_revision: str | None = None
    embedding_space_id: str | None = None
    dimension: int | None = Field(default=None, gt=0)
    image_ready: bool = False
    text_ready: bool = False
    issues: list[str] = Field(default_factory=list)
