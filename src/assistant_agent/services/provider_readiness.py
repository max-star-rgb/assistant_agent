"""Provider readiness checks and smoke output contracts."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from assistant_agent.config import ProviderConfig
from assistant_agent.services.provider_config_validation import (
    ProviderConfigIssue,
    validate_provider_config,
)
from assistant_agent.services.tool_manifest import (
    DIRECT_CHAT_CAPABILITY,
    IMAGE_GENERATION_CAPABILITY,
    IMAGE_UNDERSTANDING_CAPABILITY,
    SHOPPING_SEARCH_CAPABILITY,
    VIDEO_UNDERSTANDING_CAPABILITY,
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


def build_provider_readiness_report(config: ProviderConfig) -> ProviderReadinessReport:
    """Build readiness checks without making network calls."""

    validation = validate_provider_config(config)
    issues_by_key: dict[tuple[str, str], list[ProviderConfigIssue]] = {}
    for issue in validation.issues:
        issues_by_key.setdefault((issue.capability, issue.provider), []).append(issue)

    checks = [
        _check(IMAGE_UNDERSTANDING_CAPABILITY, config.vision_provider, config, issues_by_key),
        _check(DIRECT_CHAT_CAPABILITY, config.chat_provider, config, issues_by_key),
        _check(IMAGE_GENERATION_CAPABILITY, config.image_generation_provider, config, issues_by_key),
        _check(SHOPPING_SEARCH_CAPABILITY, config.shopping_search_provider, config, issues_by_key),
        _check(SHOPPING_SEARCH_CAPABILITY, config.shopping_compare_provider, config, issues_by_key),
        _check(VIDEO_UNDERSTANDING_CAPABILITY, config.vision_provider, config, issues_by_key),
    ]

    return ProviderReadinessReport(
        provider_mode=config.provider_mode,
        ready=all(check.status != "not_ready" for check in checks),
        checks=checks,
    )


def build_smoke_contract(
    *,
    config: ProviderConfig,
    capability: str,
    provider: str,
    success: bool,
    errors: list[dict[str, object]] | None = None,
) -> ProviderSmokeContract:
    """Build a standard smoke result wrapper."""

    readiness = _readiness_for(config, capability, provider)
    normalized_errors = errors or []
    if readiness.status == "disabled":
        return ProviderSmokeContract(
            status="skipped",
            provider=provider,
            capability=capability,
            provider_mode=config.provider_mode,
            readiness=readiness.status,
            message="Provider smoke is disabled in mock mode.",
            errors=normalized_errors,
        )
    if normalized_errors:
        return ProviderSmokeContract(
            status="failed",
            provider=provider,
            capability=capability,
            provider_mode=config.provider_mode,
            readiness=readiness.status,
            message="Provider smoke failed with structured errors.",
            errors=normalized_errors,
        )
    return ProviderSmokeContract(
        status="success" if success else "failed",
        provider=provider,
        capability=capability,
        provider_mode=config.provider_mode,
        readiness=readiness.status,
        message="Provider smoke completed." if success else "Provider smoke did not complete.",
        errors=[],
    )


def _check(
    capability: str,
    provider: str,
    config: ProviderConfig,
    issues_by_key: dict[tuple[str, str], list[ProviderConfigIssue]],
) -> ProviderReadinessCheck:
    issues = issues_by_key.get((capability, provider), [])
    if provider in _offline_providers(capability) and config.provider_mode == "real":
        status: ReadinessStatus = "disabled"
    elif provider in _offline_providers(capability):
        status = "ready"
    elif config.provider_mode != "real":
        status = "disabled"
    elif issues:
        status = "not_ready"
    else:
        status = "ready"
    return ProviderReadinessCheck(
        capability=capability,
        provider=provider,
        status=status,
        real_provider_allowed=config.provider_mode == "real",
        issues=issues,
    )


def _readiness_for(config: ProviderConfig, capability: str, provider: str) -> ProviderReadinessCheck:
    report = build_provider_readiness_report(config)
    for check in report.checks:
        if check.capability == capability and check.provider == provider:
            return check
    if provider not in _offline_providers(capability) and config.provider_mode != "real":
        status: ReadinessStatus = "disabled"
    else:
        status = "not_ready"
    return ProviderReadinessCheck(
        capability=capability,
        provider=provider,
        status=status,
        real_provider_allowed=config.provider_mode == "real",
    )


def _offline_providers(capability: str) -> set[str]:
    local_by_capability = {
        IMAGE_UNDERSTANDING_CAPABILITY: {"mock"},
        DIRECT_CHAT_CAPABILITY: {"mock"},
        IMAGE_GENERATION_CAPABILITY: {"mock"},
        SHOPPING_SEARCH_CAPABILITY: {"mock"},
        VIDEO_UNDERSTANDING_CAPABILITY: {"mock"},
    }
    return local_by_capability.get(capability, {"mock"})
