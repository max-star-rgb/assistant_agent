"""Image generation schemas."""

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


GenerationStatus = Literal["pending", "running", "succeeded", "failed"]


class ImageGenerationRequest(BaseModel):
    """图片生成工具和 Provider 适配器的输入。"""

    prompt: str = Field(
        min_length=1,
        description="希望生成的图片内容、构图、风格和关键视觉要求。",
    )
    size: str | None = Field(
        default=None,
        description=(
            "用户明确指定的图片尺寸或宽高比；尺寸使用“宽*高”，"
            "也可传 1:1、16:9、9:16、4:3、3:4；未指定时省略。"
        ),
    )
    n: int = Field(
        default=1,
        ge=1,
        le=4,
        description="用户明确要求生成的图片数量；未指定时省略。",
    )
    prompt_extend: bool = True
    watermark: bool = False
    style: str | None = Field(
        default=None,
        description="用户明确指定的视觉风格；未指定时省略。",
    )
    product_id: str | None = Field(
        default=None,
        description="作为生成依据的商品 ID；没有商品上下文时省略。",
    )
    product_title: str | None = Field(
        default=None,
        description="作为生成依据的商品标题；没有商品上下文时省略。",
    )
    product_info: dict[str, Any] = Field(
        default_factory=dict,
        description="作为生成依据的结构化商品信息；没有商品上下文时省略。",
    )
    reference_image_ids: list[str] = Field(
        default_factory=list,
        description="用户明确要求参考的图片 ID 列表。",
    )
    negative_prompt: str | None = Field(
        default=None,
        description="用户明确要求避免出现在图片中的内容。",
    )
    seed: int | None = Field(
        default=None,
        ge=0,
        description="用户明确指定的随机种子；未指定时省略。",
    )
    width: int | None = Field(
        default=None,
        ge=1,
        description="用户明确指定的图片宽度；未指定时省略。",
    )
    height: int | None = Field(
        default=None,
        ge=1,
        description="用户明确指定的图片高度；未指定时省略。",
    )
    memory_context: list[str] = Field(default_factory=list)
    user_id: str | None = None
    session_id: str | None = None

    @field_validator("prompt")
    @classmethod
    def require_non_blank_prompt(cls, value: str) -> str:
        if value.strip():
            return value
        raise ValueError("image_generation requires a non-blank prompt")


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
