"""Provider metadata registry - pure data structures with no external dependencies.

This module contains only data classes and pure functions, making it safe to
import from anywhere in the codebase (including config.py) without creating
circular dependencies.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderSpec:
    """Metadata needed to configure and validate a provider."""

    name: str
    adapter_kind: str
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

    @property
    def adapter_kind(self) -> str:
        return self.spec.adapter_kind

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


CHAT_PROVIDER_SPECS: dict[str, ProviderSpec] = {
    "mock": ProviderSpec(
        name="mock",
        adapter_kind="mock",
        requires_api_key=False,
        requires_base_url=False,
        requires_model=False,
    ),
    "openai": ProviderSpec(
        name="openai",
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
    "ark": ProviderSpec(
        name="ark",
        adapter_kind="openai_compatible",
        api_key_env="ARK_API_KEY",
        base_url_env="ARK_CHAT_BASE_URL",
        model_env="ARK_CHAT_MODEL",
        default_base_url="https://ark.cn-beijing.volces.com/api/v3",
        default_model=None,
        requires_api_key=True,
        requires_base_url=True,
        requires_model=True,
    ),
    "deepseek": ProviderSpec(
        name="deepseek",
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
        adapter_kind="mock",
        requires_api_key=False,
        requires_base_url=False,
        requires_model=False,
    ),
    "openai": ProviderSpec(
        name="openai",
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
        adapter_kind="dashscope_multimodal",
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
    "ark": ProviderSpec(
        name="ark",
        adapter_kind="ark_responses",
        api_key_env="ARK_API_KEY",
        base_url_env="ARK_VISION_BASE_URL",
        model_env="ARK_VISION_MODEL",
        default_base_url="https://ark.cn-beijing.volces.com/api/v3",
        default_model="doubao-seed-2-0-lite-260215",
        requires_api_key=True,
        requires_base_url=True,
        requires_model=True,
    ),
    "fake_realtime": ProviderSpec(
        name="fake_realtime",
        adapter_kind="fake_realtime_vision",
        model_env="FAKE_REALTIME_VISION_MODEL",
        default_model="fake-realtime-vision",
        requires_api_key=False,
        requires_base_url=False,
        requires_model=False,
    ),
}

IMAGE_GENERATION_PROVIDER_SPECS: dict[str, ProviderSpec] = {
    "mock": ProviderSpec(
        name="mock",
        adapter_kind="mock",
        requires_api_key=False,
        requires_base_url=False,
        requires_model=False,
    ),
    "openai": ProviderSpec(
        name="openai",
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
        adapter_kind="dashscope_image",
        api_key_env="QWEN_API_KEY",
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
        adapter_kind="comfyui",
        base_url_env="COMFYUI_BASE_URL",
        requires_api_key=False,
        requires_base_url=True,
        requires_model=False,
    ),
    "local": ProviderSpec(
        name="local",
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
    """Select a provider from env with the global provider-mode guardrail."""

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


def resolved_provider_values(
    provider: str,
    *,
    api_key: str | None,
    base_url: str | None,
    model: str | None,
    specs: Mapping[str, ProviderSpec],
) -> ResolvedProviderSpec:
    """Return a provider spec from values already resolved by config loading."""

    return ResolvedProviderSpec(
        spec=specs.get(provider, specs["mock"]),
        api_key=api_key,
        base_url=base_url,
        model=model,
    )


def supported_chat_providers() -> tuple[str, ...]:
    """Return supported chat provider names."""

    return supported_providers(CHAT_PROVIDER_SPECS)


def real_chat_providers() -> tuple[str, ...]:
    """Return non-mock chat provider names."""

    return real_providers(CHAT_PROVIDER_SPECS)


def select_chat_provider(value: str | None, *, allow_real: bool) -> str:
    """Select a chat provider from env with the global provider-mode guardrail."""

    return select_provider(value, allow_real=allow_real, specs=CHAT_PROVIDER_SPECS)


def resolve_chat_provider(provider: str, env: Mapping[str, str]) -> ResolvedProviderSpec:
    """Resolve selected chat provider values from environment-like data."""

    return resolve_provider(provider, _chat_provider_env_with_aliases(provider, env), CHAT_PROVIDER_SPECS)


def resolved_chat_provider(
    provider: str,
    *,
    api_key: str | None,
    base_url: str | None,
    model: str | None,
) -> ResolvedProviderSpec:
    return resolved_provider_values(
        provider,
        api_key=api_key,
        base_url=base_url,
        model=model,
        specs=CHAT_PROVIDER_SPECS,
    )


def _chat_provider_env_with_aliases(provider: str, env: Mapping[str, str]) -> Mapping[str, str]:
    if provider not in {"qwen", "ark", "deepseek"}:
        return env
    normalized = dict(env)
    if provider == "qwen":
        normalized = _qwen_env_with_api_key_aliases(
            normalized,
            "QWEN_VISION_API_KEY",
            "QWEN_IMAGE_API_KEY",
            "QWEN_IMAGE_SEARCH_API_KEY",
        )
        if not normalized.get("QWEN_CHAT_BASE_URL"):
            workspace_id = (normalized.get("QWEN_CHAT_WORKSPACE_ID") or "").strip()
            if workspace_id:
                normalized["QWEN_CHAT_BASE_URL"] = (
                    f"https://{workspace_id}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
                )
    elif provider == "ark":
        normalized = _ark_env_with_api_key_aliases(normalized, "ARK_CHAT_API_KEY")
    elif provider == "deepseek":
        normalized = _deepseek_env_with_api_key_aliases(normalized)
    return normalized


def supported_vision_providers() -> tuple[str, ...]:
    """Return supported Vision provider names."""

    return supported_providers(VISION_PROVIDER_SPECS)


def real_vision_providers() -> tuple[str, ...]:
    """Return non-mock Vision provider names."""

    return real_providers(VISION_PROVIDER_SPECS)


def select_vision_provider(value: str | None, *, allow_real: bool) -> str:
    """Select a Vision provider from env with the global provider-mode guardrail."""

    return select_provider(value, allow_real=allow_real, specs=VISION_PROVIDER_SPECS)


def resolve_vision_provider(provider: str, env: Mapping[str, str]) -> ResolvedProviderSpec:
    """Resolve selected Vision provider values from environment-like data."""

    if provider == "qwen":
        return resolve_provider(
            provider,
            _qwen_env_with_api_key_aliases(env, "QWEN_VISION_API_KEY"),
            VISION_PROVIDER_SPECS,
        )
    if provider == "ark":
        return resolve_provider(
            provider,
            _ark_env_with_api_key_aliases(env, "ARK_VISION_API_KEY"),
            VISION_PROVIDER_SPECS,
        )
    return resolve_provider(provider, env, VISION_PROVIDER_SPECS)


def resolved_vision_provider(
    provider: str,
    *,
    api_key: str | None,
    base_url: str | None,
    model: str | None,
) -> ResolvedProviderSpec:
    return resolved_provider_values(
        provider,
        api_key=api_key,
        base_url=base_url,
        model=model,
        specs=VISION_PROVIDER_SPECS,
    )


def supported_image_generation_providers() -> tuple[str, ...]:
    """Return supported image generation provider names."""

    return supported_providers(IMAGE_GENERATION_PROVIDER_SPECS)


def real_image_generation_providers() -> tuple[str, ...]:
    """Return non-mock image generation provider names."""

    return real_providers(IMAGE_GENERATION_PROVIDER_SPECS)


def select_image_generation_provider(value: str | None, *, allow_real: bool) -> str:
    """Select an image generation provider from env with the global provider-mode guardrail."""

    return select_provider(value, allow_real=allow_real, specs=IMAGE_GENERATION_PROVIDER_SPECS)


def resolve_image_generation_provider(provider: str, env: Mapping[str, str]) -> ResolvedProviderSpec:
    """Resolve selected image generation provider values from environment-like data."""

    if provider == "qwen":
        return resolve_provider(
            provider,
            _qwen_env_with_api_key_aliases(env, "QWEN_IMAGE_API_KEY"),
            IMAGE_GENERATION_PROVIDER_SPECS,
        )
    if provider == "ark":
        return resolve_provider(
            provider,
            _ark_env_with_api_key_aliases(env, "ARK_IMAGE_API_KEY"),
            IMAGE_GENERATION_PROVIDER_SPECS,
        )
    return resolve_provider(provider, env, IMAGE_GENERATION_PROVIDER_SPECS)


def resolved_image_generation_provider(
    provider: str,
    *,
    api_key: str | None,
    base_url: str | None,
    model: str | None,
) -> ResolvedProviderSpec:
    return resolved_provider_values(
        provider,
        api_key=api_key,
        base_url=base_url,
        model=model,
        specs=IMAGE_GENERATION_PROVIDER_SPECS,
    )


def _qwen_env_with_api_key_aliases(env: Mapping[str, str], *legacy_api_key_envs: str) -> dict[str, str]:
    return _env_with_api_key_aliases(env, "QWEN_API_KEY", "DASHSCOPE_API_KEY", *legacy_api_key_envs)


def _ark_env_with_api_key_aliases(env: Mapping[str, str], *legacy_api_key_envs: str) -> dict[str, str]:
    return _env_with_api_key_aliases(
        env,
        "ARK_API_KEY",
        "ARK_CHAT_API_KEY",
        "ARK_VISION_API_KEY",
        "ARK_IMAGE_API_KEY",
        *legacy_api_key_envs,
    )


def _deepseek_env_with_api_key_aliases(env: Mapping[str, str]) -> dict[str, str]:
    return _env_with_api_key_aliases(env, "DEEPSEEK_API_KEY", "DEEPSEEK_CHAT_API_KEY")


def _env_with_api_key_aliases(
    env: Mapping[str, str],
    canonical_api_key_env: str,
    *legacy_api_key_envs: str,
) -> dict[str, str]:
    normalized = dict(env)
    if normalized.get(canonical_api_key_env):
        return normalized
    for key_env in legacy_api_key_envs:
        value = normalized.get(key_env)
        if value:
            normalized[canonical_api_key_env] = value
            break
    return normalized
