"""Image generation adapter interface and mock implementation."""

from typing import Protocol

from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.generation import ImageGenerationInput, ImageGenerationRequest, ImageGenerationResult
from assistant_agent.services.provider_errors import build_provider_error
from assistant_agent.schemas.tool_ids import IMAGE_GENERATION_CAPABILITY
from assistant_agent.utils.prompting import build_image_prompt


class ImageGenerationAdapter(Protocol):
    """Adapter contract for image generation providers."""

    def generate(self, input: ImageGenerationInput) -> ImageGenerationResult:
        """Generate an image and return structured task output."""


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
            image_urls=["local://generated/poster.png"],
            request_id="mock_image_request_1",
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
            capability=IMAGE_GENERATION_CAPABILITY,
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
    provider = resolved.resolved_image_generation_provider()
    missing = provider.missing_required_env()
    if missing:
        return UnconfiguredImageGenerationAdapter(provider.provider, ", ".join(missing))
    if provider.adapter_kind == "dashscope_image":
        from assistant_agent.providers.qwen_image_generation import (
            QwenImageGenerationAdapter,
            QwenImageGenerationConfig,
        )

        return QwenImageGenerationAdapter(
            QwenImageGenerationConfig(
                api_key=provider.api_key,
                base_url=provider.base_url or "",
                model=provider.model or "",
                default_size=resolved.qwen_image_default_size,
            )
        )
    if provider.adapter_kind == "ark_image":
        from assistant_agent.providers.ark_image_generation import (
            ArkImageGenerationAdapter,
            ArkImageGenerationConfig,
        )

        return ArkImageGenerationAdapter(
            ArkImageGenerationConfig(
                api_key=provider.api_key,
                base_url=provider.base_url or "",
                model=provider.model or "",
                default_size=resolved.ark_image_default_size,
                output_format=resolved.ark_image_output_format,
            )
        )
    if resolved.provider_mode == "real":
        raise ValueError("real provider mode requires a configured image generation provider")
    return MockImageGenerationAdapter()
