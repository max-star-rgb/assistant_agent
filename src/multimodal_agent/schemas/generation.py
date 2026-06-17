"""Image generation and 3D rendering schemas."""

from typing import Any, Literal

from pydantic import BaseModel, Field


GenerationStatus = Literal["pending", "running", "succeeded", "failed"]


class ImageGenerationResult(BaseModel):
    """Result returned by an image generation adapter or tool."""

    task_id: str = Field(min_length=1)
    status: GenerationStatus
    image_url: str | None = None
    image_urls: list[str] = Field(default_factory=list)
    request_id: str | None = None
    prompt: str = Field(min_length=1)
    error: str | None = None
    provider: str = "mock"
    model: str | None = None
    output_ref: str | None = None
    prompt_used: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)
    errors: list[dict] = Field(default_factory=list)
    latency_ms: int | None = Field(default=None, ge=0)
    cost_estimate: float | None = Field(default=None, ge=0.0)


class RenderResult(BaseModel):
    """Result returned by a 3D rendering adapter or tool."""

    task_id: str = Field(min_length=1)
    status: GenerationStatus
    preview_url: str | None = None
    image_url: str | None = None
    video_url: str | None = None
    model_url: str | None = None
    render_id: str | None = None
    provider: str = "mock"
    output_ref: str | None = None
    scene_description: str | None = None
    used_inputs: dict[str, Any] = Field(default_factory=dict)
    errors: list[dict] = Field(default_factory=list)
    latency_ms: int | None = Field(default=None, ge=0)
    error: str | None = None
