"""Perception and visual understanding schemas."""

from typing import Any

from pydantic import BaseModel, Field


class VisualUnderstandingResult(BaseModel):
    """Structured result from image or video understanding."""

    objects: list[str] = Field(default_factory=list)
    colors: list[str] = Field(default_factory=list)
    materials: list[str] = Field(default_factory=list)
    scene: str | None = None
    style_tags: list[str] = Field(default_factory=list)
    text_in_media: list[str] = Field(default_factory=list)
    summary: str = Field(min_length=1)


class PerceptionBundle(BaseModel):
    """All perception outputs available to the agent for a request."""

    visual: VisualUnderstandingResult | None = None
    asr_text: str | None = None
    ocr_text: list[str] = Field(default_factory=list)
    text_summary: str | None = None


class VideoUnderstandingRequest(BaseModel):
    """Structured request for video understanding providers."""

    video_ref: str | None = None
    video_ids: list[str] = Field(default_factory=list)
    frame_refs: list[str] = Field(default_factory=list)
    context_id: str | None = None
    user_query: str | None = None
    user_id: str | None = None
    session_id: str | None = None
    max_frames: int | None = Field(default=None, ge=1)
    sample_strategy: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    memory_context: str | list[str] | None = None


class VideoUnderstandingResult(BaseModel):
    """Structured result returned by a video understanding adapter."""

    summary: str = Field(min_length=1)
    objects: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    events: list[str] = Field(default_factory=list)
    scene: str | None = None
    products: list[str] = Field(default_factory=list)
    brands: list[str] = Field(default_factory=list)
    colors: list[str] = Field(default_factory=list)
    materials: list[str] = Field(default_factory=list)
    text_in_video: list[str] = Field(default_factory=list)
    timestamps: list[dict[str, Any]] = Field(default_factory=list)
    style_tags: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    provider: str = "mock"
    model: str | None = None
    output_ref: str = Field(min_length=1)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    latency_ms: int | None = Field(default=None, ge=0)
