"""Image generation adapter interface and mock implementation."""

from typing import Any, Protocol

from pydantic import BaseModel, Field

from multimodal_agent.config import ProviderConfig
from multimodal_agent.schemas.generation import ImageGenerationResult
from multimodal_agent.services.provider_errors import build_provider_error


class ImageGenerationInput(BaseModel):
    """Input for image generation."""

    prompt: str | None = None
    style: str | None = None
    product_id: str | None = None
    product_title: str | None = None
    product_info: dict[str, Any] = Field(default_factory=dict)
    reference_image_ids: list[str] = Field(default_factory=list)
    negative_prompt: str | None = None
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    memory_context: list[str] = Field(default_factory=list)
    user_id: str | None = None
    session_id: str | None = None


ImageGenerationRequest = ImageGenerationInput


class ImageGenerationAdapter(Protocol):
    """Adapter contract for image generation providers."""

    def generate(self, input: ImageGenerationInput) -> ImageGenerationResult:
        """Generate an image and return structured task output."""


def build_image_prompt(input: ImageGenerationInput) -> str:
    """Build a deterministic prompt from product information and style."""

    from multimodal_agent.agent.prompt_builder import build_image_prompt_text

    product = input.product_title or input.product_info.get("title") or input.product_id
    style = input.style or ("日系海报" if product else None)
    product_context = product
    if input.product_info:
        product_context = product_context or input.product_info.get("summary")
    if not product and not input.prompt:
        raise ValueError("缺少生成 prompt 或商品信息，无法生成图片")

    return build_image_prompt_text(
        user_query=input.prompt or "",
        style=style,
        product_context=product_context,
        memory_context=input.memory_context,
    )


class MockImageGenerationAdapter:
    """Deterministic local image generation adapter."""

    provider = "mock"
    model = "mock-image-generation"

    def generate(self, input: ImageGenerationInput) -> ImageGenerationResult:
        prompt = build_image_prompt(input)
        return ImageGenerationResult(
            task_id="mock_image_task_1",
            status="succeeded",
            image_url="local://generated/poster.png",
            prompt=prompt,
            provider=self.provider,
            model=self.model,
            output_ref="local://generated/poster.png",
            prompt_used=prompt,
        )


class UnconfiguredImageGenerationAdapter:
    """Adapter returned when a real image provider is selected without config."""

    def __init__(self, provider: str, missing: str) -> None:
        self.provider = provider
        self.missing = missing

    def generate(self, input: ImageGenerationInput) -> ImageGenerationResult:
        prompt = input.prompt or input.style or "image generation request"
        error = build_provider_error(
            "provider_unconfigured",
            f"{self.provider} image provider is missing {self.missing}.",
            recoverable=True,
            provider=self.provider,
            capability="image_generation",
        )
        return ImageGenerationResult(
            task_id=f"{self.provider}_image_unconfigured",
            status="failed",
            prompt=prompt,
            provider=self.provider,
            model=None,
            error=f"{error.code}: {error.message}",
            errors=[
                {
                    "code": error.code,
                    "message": error.message,
                    "recoverable": error.recoverable,
                }
            ],
        )


def create_image_generation_adapter(config: ProviderConfig | None = None) -> ImageGenerationAdapter:
    """Create an image generation adapter without initializing real provider clients."""

    resolved = config or ProviderConfig.from_env()
    if resolved.image_generation_provider == "openai" and not resolved.openai_api_key:
        return UnconfiguredImageGenerationAdapter("openai", "OPENAI_API_KEY")
    if resolved.image_generation_provider == "qwen" and not resolved.qwen_api_key:
        return UnconfiguredImageGenerationAdapter("qwen", "QWEN_API_KEY")
    if resolved.image_generation_provider == "comfyui" and not resolved.comfyui_base_url:
        return UnconfiguredImageGenerationAdapter("comfyui", "COMFYUI_BASE_URL")
    if resolved.image_generation_provider == "local" and not resolved.local_image_base_url:
        return UnconfiguredImageGenerationAdapter("local", "LOCAL_IMAGE_BASE_URL")
    return MockImageGenerationAdapter()
