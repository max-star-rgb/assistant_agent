"""Image generation adapter interface and mock implementation."""

from typing import Any, Protocol

from pydantic import BaseModel, Field

from multimodal_agent.schemas.generation import ImageGenerationResult


class ImageGenerationInput(BaseModel):
    """Input for image generation."""

    prompt: str | None = None
    style: str | None = None
    product_id: str | None = None
    product_title: str | None = None
    product_info: dict[str, Any] = Field(default_factory=dict)
    reference_image_ids: list[str] = Field(default_factory=list)


class ImageGenerationAdapter(Protocol):
    """Adapter contract for image generation providers."""

    def generate(self, input: ImageGenerationInput) -> ImageGenerationResult:
        """Generate an image and return structured task output."""


def build_image_prompt(input: ImageGenerationInput) -> str:
    """Build a deterministic prompt from product information and style."""

    product = input.product_title or input.product_info.get("title") or input.product_id
    style = input.style or input.prompt
    if not product and not input.prompt:
        raise ValueError("缺少生成 prompt 或商品信息，无法生成图片")

    if product and style:
        return f"为商品「{product}」生成{style}，突出商品主体、干净构图和可用于营销展示的画面。"
    if product:
        return f"为商品「{product}」生成日系海报，突出商品主体、干净构图和可用于营销展示的画面。"
    return input.prompt or ""


class MockImageGenerationAdapter:
    """Deterministic local image generation adapter."""

    def generate(self, input: ImageGenerationInput) -> ImageGenerationResult:
        prompt = build_image_prompt(input)
        return ImageGenerationResult(
            task_id="mock_image_task_1",
            status="succeeded",
            image_url="local://generated/poster.png",
            prompt=prompt,
        )
