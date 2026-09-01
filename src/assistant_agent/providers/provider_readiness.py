"""Provider readiness checks and smoke output contracts."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from assistant_agent.config import ChatConfig, ToolConfig, VisionConfig
from assistant_agent.provider_mode import ProviderMode
from assistant_agent.providers.provider_config_validation import (
    ProviderConfigIssue,
    validate_provider_config,
)
from assistant_agent.tools.ids import (
    DIRECT_CHAT_CAPABILITY,
    IMAGE_GENERATION_CAPABILITY,
    IMAGE_UNDERSTANDING_CAPABILITY,
    SHOPPING_SEARCH_CAPABILITY,
    VIDEO_UNDERSTANDING_CAPABILITY,
    WEB_SEARCH_CAPABILITY,
)


ReadinessStatus = Literal["ready", "not_ready", "disabled"]
SmokeStatus = Literal["success", "failed", "skipped"]


class ProviderReadinessCheck(BaseModel):
    """Readiness state for one capability/provider pair."""

    capability: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    status: ReadinessStatus
    real_provider_allowed: bool
    issues: list[ProviderConfigIssue] = Field(default_factory=list)


class ProviderReadinessReport(BaseModel):
    """Provider readiness report for the current runtime config."""

    provider_mode: str = Field(min_length=1)
    ready: bool
    checks: list[ProviderReadinessCheck] = Field(default_factory=list)


class ProviderSmokeContract(BaseModel):
    """Stable smoke result shape shared by provider smoke scripts."""

    status: SmokeStatus
    provider: str = Field(min_length=1)
    capability: str = Field(min_length=1)
    provider_mode: str = Field(min_length=1)
    readiness: ReadinessStatus
    message: str = Field(min_length=1)
    errors: list[dict[str, object]] = Field(default_factory=list)


def build_provider_readiness_report(
    *,
    provider_mode: ProviderMode,
    chat_config: ChatConfig,
    vision_config: VisionConfig,
    tool_config: ToolConfig,
) -> ProviderReadinessReport:
    """Build readiness checks without making network calls."""

    validation = validate_provider_config(
        provider_mode=provider_mode,
        chat_config=chat_config,
        vision_config=vision_config,
        tool_config=tool_config,
    )
    issues_by_key: dict[tuple[str, str], list[ProviderConfigIssue]] = {}
    for issue in validation.issues:
        issues_by_key.setdefault((issue.capability, issue.provider), []).append(issue)

    checks = [
        _check(IMAGE_UNDERSTANDING_CAPABILITY, vision_config.vision_provider, provider_mode, issues_by_key),
        _check(DIRECT_CHAT_CAPABILITY, chat_config.chat_provider, provider_mode, issues_by_key),
        _check(IMAGE_GENERATION_CAPABILITY, tool_config.image_generation.image_generation_provider, provider_mode, issues_by_key),
        _check(SHOPPING_SEARCH_CAPABILITY, tool_config.shopping.shopping_search_provider, provider_mode, issues_by_key),
        _check(SHOPPING_SEARCH_CAPABILITY, tool_config.shopping.shopping_compare_provider, provider_mode, issues_by_key),
        _native_web_search_check(provider_mode, chat_config),
        _check(VIDEO_UNDERSTANDING_CAPABILITY, vision_config.vision_provider, provider_mode, issues_by_key),
    ]

    return ProviderReadinessReport(
        provider_mode=provider_mode,
        ready=all(check.status != "not_ready" for check in checks),
        checks=checks,
    )


def build_smoke_contract(
    *,
    provider_mode: ProviderMode,
    chat_config: ChatConfig,
    vision_config: VisionConfig,
    tool_config: ToolConfig,
    capability: str,
    provider: str,
    success: bool,
    errors: list[dict[str, object]] | None = None,
) -> ProviderSmokeContract:
    """Build a standard smoke result wrapper."""

    readiness = _readiness_for(
        provider_mode=provider_mode,
        chat_config=chat_config,
        vision_config=vision_config,
        tool_config=tool_config,
        capability=capability,
        provider=provider,
    )
    normalized_errors = errors or []
    if readiness.status == "disabled":
        return ProviderSmokeContract(
            status="skipped",
            provider=provider,
            capability=capability,
            provider_mode=provider_mode,
            readiness=readiness.status,
            message="Provider smoke is disabled in mock mode.",
            errors=normalized_errors,
        )
    if normalized_errors:
        return ProviderSmokeContract(
            status="failed",
            provider=provider,
            capability=capability,
            provider_mode=provider_mode,
            readiness=readiness.status,
            message="Provider smoke failed with structured errors.",
            errors=normalized_errors,
        )
    return ProviderSmokeContract(
        status="success" if success else "failed",
        provider=provider,
        capability=capability,
        provider_mode=provider_mode,
        readiness=readiness.status,
        message="Provider smoke completed." if success else "Provider smoke did not complete.",
        errors=[],
    )


def _check(
    capability: str,
    provider: str,
    provider_mode: ProviderMode,
    issues_by_key: dict[tuple[str, str], list[ProviderConfigIssue]],
) -> ProviderReadinessCheck:
    issues = issues_by_key.get((capability, provider), [])
    if provider in _offline_providers(capability) and provider_mode == "real":
        status: ReadinessStatus = "disabled"
    elif provider in _offline_providers(capability):
        status = "ready"
    elif provider_mode != "real":
        status = "disabled"
    elif issues:
        status = "not_ready"
    else:
        status = "ready"
    return ProviderReadinessCheck(
        capability=capability,
        provider=provider,
        status=status,
        real_provider_allowed=provider_mode == "real",
        issues=issues,
    )


def _readiness_for(
    *,
    provider_mode: ProviderMode,
    chat_config: ChatConfig,
    vision_config: VisionConfig,
    tool_config: ToolConfig,
    capability: str,
    provider: str,
) -> ProviderReadinessCheck:
    report = build_provider_readiness_report(
        provider_mode=provider_mode,
        chat_config=chat_config,
        vision_config=vision_config,
        tool_config=tool_config,
    )
    for check in report.checks:
        if check.capability == capability and check.provider == provider:
            return check
    if provider not in _offline_providers(capability) and provider_mode != "real":
        status: ReadinessStatus = "disabled"
    else:
        status = "not_ready"
    return ProviderReadinessCheck(
        capability=capability,
        provider=provider,
        status=status,
        real_provider_allowed=provider_mode == "real",
    )


def _native_web_search_check(
    provider_mode: ProviderMode,
    chat_config: ChatConfig,
) -> ProviderReadinessCheck:
    if provider_mode == "mock":
        return ProviderReadinessCheck(
            capability=WEB_SEARCH_CAPABILITY,
            provider="mock",
            status="ready",
            real_provider_allowed=False,
        )
    if chat_config.chat_provider != "qwen":
        return ProviderReadinessCheck(
            capability=WEB_SEARCH_CAPABILITY,
            provider=chat_config.chat_provider,
            status="disabled",
            real_provider_allowed=True,
        )
    missing = chat_config.resolved_provider().missing_required_env()
    issues: list[ProviderConfigIssue] = []
    if missing:
        issues = [
            ProviderConfigIssue(
                capability=WEB_SEARCH_CAPABILITY,
                provider="qwen",
                code="provider_unconfigured",
                message=(
                    "Bailian native web search requires a configured "
                    "qwen-provider Chat endpoint."
                ),
                missing=missing,
            ),
        ]
    return ProviderReadinessCheck(
        capability=WEB_SEARCH_CAPABILITY,
        provider="qwen",
        status="not_ready" if issues else "ready",
        real_provider_allowed=True,
        issues=issues,
    )


def _offline_providers(capability: str) -> set[str]:
    local_by_capability = {
        IMAGE_UNDERSTANDING_CAPABILITY: {"mock"},
        DIRECT_CHAT_CAPABILITY: {"mock"},
        IMAGE_GENERATION_CAPABILITY: {"mock"},
        SHOPPING_SEARCH_CAPABILITY: {"mock"},
        WEB_SEARCH_CAPABILITY: {"mock"},
        VIDEO_UNDERSTANDING_CAPABILITY: {"mock"},
    }
    return local_by_capability.get(capability, {"mock"})
