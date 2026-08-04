"""Plugin-private image generation adapter interface and implementations."""

from pathlib import Path
from typing import Protocol

from assistant_agent.config import ProviderConfig
from assistant_agent.providers.provider_errors import ProviderAdapterError, build_provider_error
from assistant_agent.providers.prompting import build_image_prompt
from assistant_agent.runtime.generated_artifacts import (
    GENERATED_ARTIFACT_DIR,
    GENERATED_ARTIFACT_PUBLIC_PREFIX,
    generated_artifact_payload,
)
from assistant_agent.tools.plugins.builtin.image_generation.models import ImageGenerationRequest, ImageGenerationResult
from assistant_agent.tools.ids import IMAGE_GENERATION_CAPABILITY


class ImageGenerationAdapter(Protocol):
    """Adapter contract for image generation providers."""

    def generate(self, input: ImageGenerationRequest) -> ImageGenerationResult:
        """Generate an image and return structured task output."""


class MockImageGenerationAdapter:
    """Deterministic local image generation adapter."""

    provider = "mock"
    model = "mock-image-generation"

    def generate(self, input: ImageGenerationRequest) -> ImageGenerationResult:
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


class LocalFixtureImageGenerationAdapter:
    """Return one existing managed image without invoking a Provider."""

    provider = "local_fixture"
    model = "local-managed-artifact"

    def __init__(
        self,
        fixture_id: str,
        *,
        artifact_dir: Path = GENERATED_ARTIFACT_DIR,
    ) -> None:
        self.fixture_id = fixture_id.strip()
        self.artifact_dir = artifact_dir

    def generate(self, input: ImageGenerationRequest) -> ImageGenerationResult:
        fixture_path = Path(self.fixture_id)
        if not self.fixture_id or fixture_path.name != self.fixture_id:
            raise ProviderAdapterError(
                "provider_unavailable",
                "configured local image fixture is invalid",
            )

        output_ref = f"{GENERATED_ARTIFACT_PUBLIC_PREFIX.rstrip('/')}/{self.fixture_id}"
        if generated_artifact_payload(output_ref, artifact_dir=self.artifact_dir) is None:
            raise ProviderAdapterError(
                "provider_unavailable",
                "configured local image fixture is unavailable",
            )

        return ImageGenerationResult(
            task_id=f"local_fixture:{fixture_path.stem}",
            status="succeeded",
            image_url=output_ref,
            image_urls=[output_ref],
            download_url=output_ref,
            download_urls=[output_ref],
            image_id=[fixture_path.stem],
            request_id=f"local_fixture:{fixture_path.stem}",
            prompt=input.prompt,
            provider=self.provider,
            model=self.model,
            output_ref=output_ref,
            prompt_used=input.prompt,
        )


class UnconfiguredImageGenerationAdapter:
    """Adapter returned when a real image provider is selected without config."""

    def __init__(self, provider: str, missing: str) -> None:
        self.provider = provider
        self.missing = missing

    def generate(self, input: ImageGenerationRequest) -> ImageGenerationResult:
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
