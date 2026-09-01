"""Provider selection helpers for optional real adapters."""

from assistant_agent.config import VisionConfig
from assistant_agent.media.vision.real_vision_adapter import (
    DashScopeVisionProviderAdapter,
    HttpVisionProviderAdapter,
    RealVisionProviderConfig,
)
from assistant_agent.media.vision.vision_adapter import MockVisionUnderstandingAdapter, VisionUnderstandingAdapter
from assistant_agent.provider_mode import ProviderMode


def create_vision_adapter(
    config: VisionConfig,
    *,
    provider_mode: ProviderMode,
) -> VisionUnderstandingAdapter:
    """Create the configured vision adapter.

    Defaults to the local deterministic mock adapter.
    """

    if provider_mode != "real":
        return MockVisionUnderstandingAdapter()
    provider = config.resolved_provider()
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
