"""Provider configuration validation without initializing provider clients."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, Field

from multimodal_agent.config import ProviderConfig
from multimodal_agent.services.provider_errors import ProviderError, build_provider_error


ValidationSeverity = Literal["error", "warning"]


class ProviderConfigIssue(BaseModel):
    """A redacted provider configuration validation issue."""

    capability: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    missing: list[str] = Field(default_factory=list)
    severity: ValidationSeverity = "error"


class ProviderConfigValidationResult(BaseModel):
    """Validation result for all selected provider configurations."""

    runtime_profile: str = Field(min_length=1)
    valid: bool
    issues: list[ProviderConfigIssue] = Field(default_factory=list)

    @property
    def errors(self) -> list[ProviderConfigIssue]:
        """Return blocking issues."""

        return [issue for issue in self.issues if issue.severity == "error"]


def validate_provider_config(config: ProviderConfig) -> ProviderConfigValidationResult:
    """Validate selected provider config without making provider calls."""

    issues: list[ProviderConfigIssue] = []

    _add_issue_if_missing(
        issues,
        capability="image_understanding",
        provider=config.vision_provider,
        missing=_vision_missing(config),
    )
    _add_issue_if_missing(
        issues,
        capability="direct_chat",
        provider=config.chat_provider,
        missing=_chat_missing(config),
    )
    _add_issue_if_missing(
        issues,
        capability="image_generation",
        provider=config.image_generation_provider,
        missing=_image_generation_missing(config),
    )
    _add_issue_if_missing(
        issues,
        capability="product_search",
        provider=config.product_search_provider,
        missing=_product_search_missing(config),
    )
    _add_issue_if_missing(
        issues,
        capability="price_compare",
        provider=config.price_compare_provider,
        missing=_price_compare_missing(config),
    )
    _add_issue_if_missing(
        issues,
        capability="render_3d",
        provider=config.render_provider,
        missing=_render_missing(config),
    )
    _add_issue_if_missing(
        issues,
        capability="video_understanding",
        provider=config.video_provider,
        missing=_video_missing(config),
    )

    return ProviderConfigValidationResult(
        runtime_profile=config.runtime_profile.name,
        valid=not any(issue.severity == "error" for issue in issues),
        issues=issues,
    )


def validation_issue_to_provider_error(issue: ProviderConfigIssue) -> ProviderError:
    """Convert a validation issue into the shared provider error shape."""

    return build_provider_error(
        issue.code,
        issue.message,
        detail={"missing": issue.missing, "severity": issue.severity},
        recoverable=True,
        provider=issue.provider,
        capability=issue.capability,
    )


def _add_issue_if_missing(
    issues: list[ProviderConfigIssue],
    *,
    capability: str,
    provider: str,
    missing: Iterable[str],
) -> None:
    missing_values = list(missing)
    if not missing_values:
        return
    issues.append(
        ProviderConfigIssue(
            capability=capability,
            provider=provider,
            code="provider_unconfigured",
            message=f"{provider} provider for {capability} is missing required configuration.",
            missing=missing_values,
        )
    )


def _vision_missing(config: ProviderConfig) -> list[str]:
    return config.resolved_vision_provider().missing_required_env()


def _chat_missing(config: ProviderConfig) -> list[str]:
    return config.resolved_chat_provider().missing_required_env()


def _image_generation_missing(config: ProviderConfig) -> list[str]:
    return config.resolved_image_generation_provider().missing_required_env()


def _product_search_missing(config: ProviderConfig) -> list[str]:
    if config.product_search_provider == "local_json":
        return _missing(("PRODUCT_SEARCH_LOCAL_PATH", config.product_search_local_path))
    if config.product_search_provider == "http":
        return _missing(
            ("PRODUCT_SEARCH_BASE_URL", config.product_search_base_url),
            ("PRODUCT_SEARCH_API_KEY", config.product_search_api_key),
        )
    return []


def _price_compare_missing(config: ProviderConfig) -> list[str]:
    if config.price_compare_provider == "http":
        return _missing(
            ("PRICE_COMPARE_BASE_URL", config.price_compare_base_url),
            ("PRICE_COMPARE_API_KEY", config.price_compare_api_key),
        )
    return []


def _render_missing(config: ProviderConfig) -> list[str]:
    if config.render_provider == "http":
        return _missing(
            ("RENDER_BASE_URL", config.render_base_url),
            ("RENDER_API_KEY", config.render_api_key),
        )
    return []


def _video_missing(config: ProviderConfig) -> list[str]:
    if config.video_provider == "http":
        return _missing(
            ("VIDEO_UNDERSTANDING_BASE_URL", config.video_understanding_base_url),
            ("VIDEO_UNDERSTANDING_API_KEY", config.video_understanding_api_key),
        )
    return []


def _missing(*items: tuple[str, str | None]) -> list[str]:
    return [name for name, value in items if not value]

