"""Provider selection helpers for optional real adapters."""

from assistant_agent.config import ProviderConfig
from assistant_agent.media.vision.real_vision_adapter import (
    DashScopeVisionProviderAdapter,
    HttpVisionProviderAdapter,
    RealVisionProviderConfig,
)
from assistant_agent.media.vision.vision_adapter import MockVisionUnderstandingAdapter, VisionUnderstandingAdapter


def create_vision_adapter(config: ProviderConfig | None = None) -> VisionUnderstandingAdapter:
    """Create the configured vision adapter.

    Defaults to the local deterministic mock adapter.
    """

    resolved_config = config or ProviderConfig.from_env()
    provider = resolved_config.resolved_vision_provider()
    if provider.adapter_kind == "ark_responses":
        from assistant_agent.providers.ark_vision import ArkVisionProviderAdapter, ArkVisionProviderConfig

        return ArkVisionProviderAdapter(
            ArkVisionProviderConfig(
                provider=provider.provider,
                api_key=provider.api_key,
                base_url=provider.base_url or "",
                model=provider.model or "",
            )
        )
    if provider.adapter_kind == "openai_compatible":
        return HttpVisionProviderAdapter(
            RealVisionProviderConfig(
                provider=provider.provider,
                api_key=provider.api_key,
                base_url=provider.base_url or "",
                model=provider.model or "",
            )
        )
    if provider.adapter_kind == "dashscope_multimodal":
        return DashScopeVisionProviderAdapter(
            RealVisionProviderConfig(
                provider=provider.provider,
                api_key=provider.api_key,
                base_url=provider.base_url or "",
                model=provider.model or "",
            )
        )
    return MockVisionUnderstandingAdapter()
