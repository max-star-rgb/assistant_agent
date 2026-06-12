"""Perception and visual understanding schemas."""

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
