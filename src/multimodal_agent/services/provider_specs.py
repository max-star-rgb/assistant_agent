"""Central provider metadata registry."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


AdapterKind = str


@dataclass(frozen=True)
class ProviderSpec:
    """Metadata needed to configure and validate a provider."""

    name: str
    capability: str
    provider_env: str
    adapter_kind: AdapterKind
    api_key_env: str | None = None
    base_url_env: str | None = None
    model_env: str | None = None
    default_base_url: str | None = None
    default_model: str | None = None
    requires_api_key: bool = True
    requires_base_url: bool = False
    requires_model: bool = False
    placeholder_base_urls: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResolvedProviderSpec:
    """A selected provider spec with env-resolved values."""

    spec: ProviderSpec
    api_key: str | None
    base_url: str | None
    model: str | None

    @property
    def provider(self) -> str:
        return self.spec.name

    def missing_required_env(self) -> list[str]:
        missing: list[str] = []
        if self.spec.requires_api_key and self.spec.api_key_env and not self.api_key:
            missing.append(self.spec.api_key_env)
        if (
            self.spec.requires_base_url
            and self.spec.base_url_env
            and (not self.base_url or self.base_url in self.spec.placeholder_base_urls)
        ):
            missing.append(self.spec.base_url_env)
        if self.spec.requires_model and self.spec.model_env and not self.model:
            missing.append(self.spec.model_env)
        return missing


CHAT_PROVIDER_ENV = "MULTIMODAL_AGENT_CHAT_PROVIDER"
VISION_PROVIDER_ENV = "MULTIMODAL_AGENT_VISION_PROVIDER"
IMAGE_GENERATION_PROVIDER_ENV = "MULTIMODAL_AGENT_IMAGE_PROVIDER"

CHAT_PROVIDER_SPECS: dict[str, ProviderSpec] = {
    "mock": ProviderSpec(
        name="mock",
        capability="direct_chat",
        provider_env=CHAT_PROVIDER_ENV,
        adapter_kind="mock",
        requires_api_key=False,
        requires_base_url=False,
        requires_model=False,
    ),
    "openai": ProviderSpec(
        name="openai",
        capability="direct_chat",
        provider_env=CHAT_PROVIDER_ENV,
        adapter_kind="openai_compatible",
        api_key_env="OPENAI_API_KEY",
        base_url_env="OPENAI_CHAT_BASE_URL",
        model_env="OPENAI_CHAT_MODEL",
        default_base_url="https://api.openai.com/v1",
        default_model="gpt-4o-mini",
        requires_api_key=True,
        requires_base_url=True,
        requires_model=True,
    ),
    "qwen": ProviderSpec(
        name="qwen",
        capability="direct_chat",
        provider_env=CHAT_PROVIDER_ENV,
        adapter_kind="openai_compatible",
        api_key_env="QWEN_API_KEY",
        base_url_env="QWEN_CHAT_BASE_URL",
        model_env="QWEN_CHAT_MODEL",
        default_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        default_model="qwen-plus",
        requires_api_key=True,
        requires_base_url=True,
        requires_model=True,
    ),
    "deepseek": ProviderSpec(
        name="deepseek",
        capability="direct_chat",
        provider_env=CHAT_PROVIDER_ENV,
        adapter_kind="openai_compatible",
        api_key_env="DEEPSEEK_API_KEY",
        base_url_env="DEEPSEEK_CHAT_BASE_URL",
        model_env="DEEPSEEK_CHAT_MODEL",
        default_base_url="https://api.deepseek.com/v1",
        default_model="deepseek-chat",
        requires_api_key=True,
        requires_base_url=True,
        requires_model=True,
    ),
    "local": ProviderSpec(
        name="local",
        capability="direct_chat",
        provider_env=CHAT_PROVIDER_ENV,
        adapter_kind="local_http",
        base_url_env="LOCAL_CHAT_BASE_URL",
        model_env="LOCAL_CHAT_MODEL",
        default_model="local-chat",
        requires_api_key=False,
        requires_base_url=True,
        requires_model=True,
    ),
}

VISION_PROVIDER_SPECS: dict[str, ProviderSpec] = {
    "mock": ProviderSpec(
        name="mock",
        capability="image_understanding",
        provider_env=VISION_PROVIDER_ENV,
        adapter_kind="mock",
        requires_api_key=False,
        requires_base_url=False,
        requires_model=False,
    ),
    "openai": ProviderSpec(
        name="openai",
        capability="image_understanding",
        provider_env=VISION_PROVIDER_ENV,
        adapter_kind="openai_compatible",
        api_key_env="OPENAI_API_KEY",
        base_url_env="OPENAI_VISION_BASE_URL",
        model_env="OPENAI_VISION_MODEL",
        default_base_url="https://api.openai.com/v1",
        default_model="gpt-4o-mini",
        requires_api_key=True,
        requires_base_url=True,
        requires_model=True,
    ),
    "qwen": ProviderSpec(
        name="qwen",
        capability="image_understanding",
        provider_env=VISION_PROVIDER_ENV,
        adapter_kind="openai_compatible",
        api_key_env="QWEN_API_KEY",
        base_url_env="QWEN_VISION_BASE_URL",
        model_env="QWEN_VISION_MODEL",
        default_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        default_model="qwen-vl-plus",
        requires_api_key=True,
        requires_base_url=True,
        requires_model=True,
    ),
    "seed": ProviderSpec(
        name="seed",
        capability="image_understanding",
        provider_env=VISION_PROVIDER_ENV,
        adapter_kind="openai_compatible",
        api_key_env="SEED_API_KEY",
        base_url_env="SEED_VISION_BASE_URL",
        model_env="SEED_VISION_MODEL",
        default_base_url="https://api.seed.example/v1/vision",
        default_model="seed-vision",
        requires_api_key=True,
        requires_base_url=True,
        requires_model=True,
        placeholder_base_urls=("https://api.seed.example/v1/vision",),
    ),
}

IMAGE_GENERATION_PROVIDER_SPECS: dict[str, ProviderSpec] = {
    "mock": ProviderSpec(
        name="mock",
        capability="image_generation",
        provider_env=IMAGE_GENERATION_PROVIDER_ENV,
        adapter_kind="mock",
        requires_api_key=False,
        requires_base_url=False,
        requires_model=False,
    ),
    "openai": ProviderSpec(
        name="openai",
        capability="image_generation",
        provider_env=IMAGE_GENERATION_PROVIDER_ENV,
        adapter_kind="openai_image",
        api_key_env="OPENAI_API_KEY",
        model_env="OPENAI_IMAGE_MODEL",
        default_model="gpt-image-1",
        requires_api_key=True,
        requires_base_url=False,
        requires_model=True,
    ),
    "qwen": ProviderSpec(
        name="qwen",
        capability="image_generation",
        provider_env=IMAGE_GENERATION_PROVIDER_ENV,
        adapter_kind="dashscope_image",
        api_key_env="DASHSCOPE_API_KEY",
        base_url_env="QWEN_IMAGE_BASE_URL",
        model_env="QWEN_IMAGE_MODEL",
        default_base_url="https://dashscope.aliyuncs.com/api/v1",
        default_model="qwen-image-2.0-pro",
        requires_api_key=True,
        requires_base_url=True,
        requires_model=True,
    ),
    "ark": ProviderSpec(
        name="ark",
        capability="image_generation",
        provider_env=IMAGE_GENERATION_PROVIDER_ENV,
        adapter_kind="ark_image",
        api_key_env="ARK_API_KEY",
        base_url_env="ARK_IMAGE_BASE_URL",
        model_env="ARK_IMAGE_MODEL",
        requires_api_key=True,
        requires_base_url=True,
        requires_model=True,
    ),
    "comfyui": ProviderSpec(
        name="comfyui",
        capability="image_generation",
        provider_env=IMAGE_GENERATION_PROVIDER_ENV,
        adapter_kind="comfyui",
        base_url_env="COMFYUI_BASE_URL",
        requires_api_key=False,
        requires_base_url=True,
        requires_model=False,
    ),
    "local": ProviderSpec(
        name="local",
        capability="image_generation",
        provider_env=IMAGE_GENERATION_PROVIDER_ENV,
        adapter_kind="local_http",
        base_url_env="LOCAL_IMAGE_BASE_URL",
        model_env="LOCAL_IMAGE_MODEL",
        default_model="local-image",
        requires_api_key=False,
        requires_base_url=True,
        requires_model=True,
    ),
}


def supported_providers(specs: Mapping[str, ProviderSpec]) -> tuple[str, ...]:
    """Return supported provider names for a spec group."""

    return tuple(specs)


def real_providers(specs: Mapping[str, ProviderSpec]) -> tuple[str, ...]:
    """Return non-mock provider names for a spec group."""

    return tuple(name for name in specs if name != "mock")


def select_provider(value: str | None, *, allow_real: bool, specs: Mapping[str, ProviderSpec]) -> str:
    """Select a provider from env with runtime profile guardrails."""

    if allow_real and value in specs:
        return value
    return "mock"


def resolve_provider(provider: str, env: Mapping[str, str], specs: Mapping[str, ProviderSpec]) -> ResolvedProviderSpec:
    """Resolve selected provider values from environment-like data."""

    spec = specs.get(provider, specs["mock"])
    return ResolvedProviderSpec(
        spec=spec,
        api_key=env.get(spec.api_key_env) if spec.api_key_env else None,
        base_url=env.get(spec.base_url_env, spec.default_base_url) if spec.base_url_env else spec.default_base_url,
        model=env.get(spec.model_env, spec.default_model) if spec.model_env else spec.default_model,
    )


def supported_chat_providers() -> tuple[str, ...]:
    """Return supported chat provider names."""

    return supported_providers(CHAT_PROVIDER_SPECS)


def real_chat_providers() -> tuple[str, ...]:
    """Return non-mock chat provider names."""

    return real_providers(CHAT_PROVIDER_SPECS)


def select_chat_provider(value: str | None, *, allow_real: bool) -> str:
    """Select a chat provider from env with runtime profile guardrails."""

    return select_provider(value, allow_real=allow_real, specs=CHAT_PROVIDER_SPECS)


def resolve_chat_provider(provider: str, env: Mapping[str, str]) -> ResolvedProviderSpec:
    """Resolve selected chat provider values from environment-like data."""

    return resolve_provider(provider, env, CHAT_PROVIDER_SPECS)


def supported_vision_providers() -> tuple[str, ...]:
    """Return supported Vision provider names."""

    return supported_providers(VISION_PROVIDER_SPECS)


def real_vision_providers() -> tuple[str, ...]:
    """Return non-mock Vision provider names."""

    return real_providers(VISION_PROVIDER_SPECS)


def select_vision_provider(value: str | None, *, allow_real: bool) -> str:
    """Select a Vision provider from env with runtime profile guardrails."""

    return select_provider(value, allow_real=allow_real, specs=VISION_PROVIDER_SPECS)


def resolve_vision_provider(provider: str, env: Mapping[str, str]) -> ResolvedProviderSpec:
    """Resolve selected Vision provider values from environment-like data."""

    return resolve_provider(provider, env, VISION_PROVIDER_SPECS)


def supported_image_generation_providers() -> tuple[str, ...]:
    """Return supported image generation provider names."""

    return supported_providers(IMAGE_GENERATION_PROVIDER_SPECS)


def real_image_generation_providers() -> tuple[str, ...]:
    """Return non-mock image generation provider names."""

    return real_providers(IMAGE_GENERATION_PROVIDER_SPECS)


def select_image_generation_provider(value: str | None, *, allow_real: bool) -> str:
    """Select an image generation provider from env with runtime profile guardrails."""

    return select_provider(value, allow_real=allow_real, specs=IMAGE_GENERATION_PROVIDER_SPECS)


def resolve_image_generation_provider(provider: str, env: Mapping[str, str]) -> ResolvedProviderSpec:
    """Resolve selected image generation provider values from environment-like data."""

    return resolve_provider(provider, env, IMAGE_GENERATION_PROVIDER_SPECS)
