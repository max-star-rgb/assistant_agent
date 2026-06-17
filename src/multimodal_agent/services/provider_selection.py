"""Provider selection helpers for optional real adapters."""

from multimodal_agent.config import ProviderConfig
from multimodal_agent.services.real_vision_adapter import HttpVisionProviderAdapter, RealVisionProviderConfig
from multimodal_agent.services.vision_adapter import MockVisionUnderstandingAdapter, VisionUnderstandingAdapter


def create_vision_adapter(config: ProviderConfig | None = None) -> VisionUnderstandingAdapter:
    """Create the configured vision adapter.

    Defaults to the local deterministic mock adapter.
    """

    resolved_config = config or ProviderConfig.from_env()
    provider = resolved_config.resolved_vision_provider()
    if provider.spec.adapter_kind == "openai_compatible":
        return HttpVisionProviderAdapter(
            RealVisionProviderConfig(
                provider=provider.provider,
                api_key=provider.api_key,
                base_url=provider.base_url or "",
                model=provider.model or "",
            )
        )
    return MockVisionUnderstandingAdapter()
