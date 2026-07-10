"""Backward compatibility shim - imports from schemas.provider_specs.

This module has been moved to assistant_agent.schemas.provider_specs
to avoid circular dependencies with config.py.
"""

from assistant_agent.schemas.provider_specs import (  # noqa: F401
    AdapterKind,
    CHAT_PROVIDER_ENV,
    CHAT_PROVIDER_SPECS,
    IMAGE_GENERATION_PROVIDER_ENV,
    IMAGE_GENERATION_PROVIDER_SPECS,
    IMAGE_GENERATION_CAPABILITIES,
    ProviderSpec,
    ProviderCapabilities,
    OPENAI_COMPATIBLE_CHAT_CAPABILITIES,
    LOCAL_CHAT_CAPABILITIES,
    MOCK_CHAT_CAPABILITIES,
    real_chat_providers,
    real_image_generation_providers,
    real_providers,
    real_vision_providers,
    ResolvedProviderSpec,
    resolve_chat_provider,
    resolve_image_generation_provider,
    resolve_provider,
    resolve_vision_provider,
    select_chat_provider,
    select_image_generation_provider,
    select_provider,
    select_vision_provider,
    supported_chat_providers,
    supported_image_generation_providers,
    supported_providers,
    supported_vision_providers,
    VISION_PROVIDER_ENV,
    VISION_TEXT_IMAGE_CAPABILITIES,
    VISION_PROVIDER_SPECS,
)
