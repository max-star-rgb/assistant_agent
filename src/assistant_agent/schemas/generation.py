"""Image generation schemas."""

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


GenerationStatus = Literal["pending", "running", "succeeded", "failed"]


class ImageGenerationInput(BaseModel):
    """图片生成工具和 Provider 适配器的输入。"""

    prompt: str | None = None
    size: str | None = None
    n: int = Field(default=1, ge=1, le=4)
    prompt_extend: bool = True
    watermark: bool = False
    style: str | None = None
    product_id: str | None = None
    product_title: str | None = None
    product_info: dict[str, Any] = Field(default_factory=dict)
    reference_image_ids: list[str] = Field(default_factory=list)
    negative_prompt: str | None = None
    seed: int | None = Field(default=None, ge=0)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    memory_context: list[str] = Field(default_factory=list)
    user_id: str | None = None
    session_id: str | None = None

    @model_validator(mode="after")
    def require_generation_source(self) -> "ImageGenerationInput":
        if any(
            isinstance(value, str) and value.strip()
            for value in (self.prompt, self.product_id, self.product_title)
        ) or self.product_info:
            return self
        raise ValueError("image_generation requires prompt or product information")


ImageGenerationRequest = ImageGenerationInput


class ImageGenerationResult(BaseModel):
    """Result returned by an image generation adapter or tool."""

    task_id: str = Field(min_length=1)
    status: GenerationStatus
    image_url: str | None = None
    image_urls: list[str] = Field(default_factory=list)
    download_url: str | None = None
    download_urls: list[str] = Field(default_factory=list)
    provider_image_urls: list[str] = Field(default_factory=list)
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
