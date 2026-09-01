"""Provider configuration validation without initializing provider clients."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, Field

from assistant_agent.config import ChatConfig, ToolConfig, VisionConfig
from assistant_agent.provider_mode import ProviderMode
from assistant_agent.providers.provider_errors import ProviderError, build_provider_error
from assistant_agent.tools.ids import (
    DIRECT_CHAT_CAPABILITY,
    IMAGE_GENERATION_CAPABILITY,
    IMAGE_UNDERSTANDING_CAPABILITY,
    SHOPPING_SEARCH_CAPABILITY,
    VIDEO_UNDERSTANDING_CAPABILITY,
)


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

    provider_mode: str = Field(min_length=1)
    valid: bool
    issues: list[ProviderConfigIssue] = Field(default_factory=list)

    @property
    def errors(self) -> list[ProviderConfigIssue]:
        """Return blocking issues."""

        return [issue for issue in self.issues if issue.severity == "error"]


def validate_provider_config(
    *,
    provider_mode: ProviderMode,
    chat_config: ChatConfig,
    vision_config: VisionConfig,
    tool_config: ToolConfig,
) -> ProviderConfigValidationResult:
    """Validate selected provider config without making provider calls."""

    issues: list[ProviderConfigIssue] = []

    _add_issue_if_missing(
        issues,
        capability=IMAGE_UNDERSTANDING_CAPABILITY,
        provider=vision_config.vision_provider,
        missing=_vision_missing(vision_config),
    )
    _add_issue_if_missing(
        issues,
        capability=DIRECT_CHAT_CAPABILITY,
        provider=chat_config.chat_provider,
        missing=_chat_missing(chat_config),
    )
    _add_issue_if_missing(
        issues,
        capability=IMAGE_GENERATION_CAPABILITY,
        provider=tool_config.image_generation.image_generation_provider,
        missing=_image_generation_missing(tool_config),
    )
    _add_issue_if_missing(
        issues,
        capability=SHOPPING_SEARCH_CAPABILITY,
        provider=tool_config.shopping.shopping_search_provider,
        missing=_shopping_search_missing(tool_config),
    )
    _add_issue_if_missing(
        issues,
        capability=SHOPPING_SEARCH_CAPABILITY,
        provider=tool_config.shopping.shopping_compare_provider,
        missing=_shopping_compare_missing(tool_config),
    )
    _add_issue_if_missing(
        issues,
        capability=VIDEO_UNDERSTANDING_CAPABILITY,
        provider=vision_config.vision_provider,
        missing=_vision_missing(vision_config),
    )

    return ProviderConfigValidationResult(
        provider_mode=provider_mode,
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


def _vision_missing(config: VisionConfig) -> list[str]:
    return config.resolved_provider().missing_required_env()


def _chat_missing(config: ChatConfig) -> list[str]:
    return config.resolved_provider().missing_required_env()


def _image_generation_missing(config: ToolConfig) -> list[str]:
    return config.image_generation.resolved_provider().missing_required_env()


def _shopping_search_missing(config: ToolConfig) -> list[str]:
    if config.shopping.shopping_search_provider == "http":
        return _missing(
            ("SHOPPING_SEARCH_BASE_URL", config.shopping.shopping_search_base_url),
            ("SHOPPING_SEARCH_API_KEY", config.shopping.shopping_search_api_key),
        )
    return []


def _shopping_compare_missing(config: ToolConfig) -> list[str]:
    if config.shopping.shopping_compare_provider == "http":
        return _missing(
            ("SHOPPING_COMPARE_BASE_URL", config.shopping.shopping_compare_base_url),
            ("SHOPPING_COMPARE_API_KEY", config.shopping.shopping_compare_api_key),
        )
    return []


def _missing(*items: tuple[str, str | None]) -> list[str]:
    return [name for name, value in items if not value]
