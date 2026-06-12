"""Image generation and 3D rendering schemas."""

from typing import Literal

from pydantic import BaseModel, Field


GenerationStatus = Literal["pending", "running", "succeeded", "failed"]


class ImageGenerationResult(BaseModel):
    """Result returned by an image generation adapter or tool."""

    task_id: str = Field(min_length=1)
    status: GenerationStatus
    image_url: str | None = None
    prompt: str = Field(min_length=1)
    error: str | None = None


class RenderResult(BaseModel):
    """Result returned by a 3D rendering adapter or tool."""

    task_id: str = Field(min_length=1)
    status: GenerationStatus
    preview_url: str | None = None
    model_url: str | None = None
    error: str | None = None
