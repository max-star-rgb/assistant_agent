"""Provider selection helpers for optional real adapters."""

from multimodal_agent.config import ProviderConfig
from multimodal_agent.services.real_vision_adapter import HttpVisionProviderAdapter, RealVisionProviderConfig
from multimodal_agent.services.vision_adapter import MockVisionUnderstandingAdapter, VisionUnderstandingAdapter


def create_vision_adapter(config: ProviderConfig | None = None) -> VisionUnderstandingAdapter:
    """Create the configured vision adapter.

    Defaults to the local deterministic mock adapter.
    """

    resolved_config = config or ProviderConfig.from_env()
    if resolved_config.vision_provider == "openai":
        return HttpVisionProviderAdapter(
            RealVisionProviderConfig(
                provider="openai",
                api_key=resolved_config.openai_api_key,
                base_url=resolved_config.openai_vision_base_url,
                model=resolved_config.openai_vision_model,
            )
        )
    if resolved_config.vision_provider == "qwen":
        return HttpVisionProviderAdapter(
            RealVisionProviderConfig(
                provider="qwen",
                api_key=resolved_config.qwen_api_key,
                base_url=resolved_config.qwen_vision_base_url,
                model=resolved_config.qwen_vision_model,
            )
        )
    if resolved_config.vision_provider == "seed":
        return HttpVisionProviderAdapter(
            RealVisionProviderConfig(
                provider="seed",
                api_key=resolved_config.seed_api_key,
                base_url=resolved_config.seed_vision_base_url,
                model=resolved_config.seed_vision_model,
            )
        )
    return MockVisionUnderstandingAdapter()
