"""Redacted provider diagnostics and safety defaults."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from assistant_agent.config import ProviderConfig
from assistant_agent.services.provider_config_validation import validate_provider_config
from assistant_agent.services.provider_errors import sanitize_error_detail, sanitize_error_message
from assistant_agent.services.provider_policy import ProviderExecutionPolicy
from assistant_agent.services.provider_readiness import build_provider_readiness_report


class ProviderSelectionSummary(BaseModel):
    """Redacted selected provider summary for diagnostics."""

    capability: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    readiness: str = Field(min_length=1)


class ProviderSafetyDefaultsSummary(BaseModel):
    """Provider safety defaults exposed for diagnostics."""

    allow_mock_fallback: bool
    allow_partial_result: bool
    max_retries: int
    retry_on: list[str]
    timeout_seconds_by_capability: dict[str, float]


class ProviderDiagnosticsSummary(BaseModel):
    """Redacted diagnostics summary for support and readiness checks."""

    runtime_profile: str = Field(min_length=1)
    allows_real_providers: bool
    selected_providers: list[ProviderSelectionSummary] = Field(default_factory=list)
    validation_valid: bool
    validation_issue_count: int
    safety_defaults: ProviderSafetyDefaultsSummary
    notes: list[str] = Field(default_factory=list)


def build_provider_diagnostics_summary(
    config: ProviderConfig,
    *,
    execution_policy: ProviderExecutionPolicy | None = None,
    extra_notes: list[object] | None = None,
) -> ProviderDiagnosticsSummary:
    """Build a redacted provider diagnostics summary without provider calls."""

    readiness = build_provider_readiness_report(config)
    validation = validate_provider_config(config)
    policy = execution_policy or ProviderExecutionPolicy()

    return ProviderDiagnosticsSummary(
        runtime_profile=config.runtime_profile.name,
        allows_real_providers=config.runtime_profile.allows_real_providers,
        selected_providers=[
            ProviderSelectionSummary(
                capability=check.capability,
                provider=check.provider,
                readiness=check.status,
            )
            for check in readiness.checks
        ],
        validation_valid=validation.valid,
        validation_issue_count=len(validation.issues),
        safety_defaults=ProviderSafetyDefaultsSummary(
            allow_mock_fallback=policy.fallback.allow_mock_fallback,
            allow_partial_result=policy.fallback.allow_partial_result,
            max_retries=policy.retry.max_retries,
            retry_on=[sanitize_error_message(item) for item in policy.retry.retry_on],
            timeout_seconds_by_capability={
                capability: policy.timeout.for_capability(capability)
                for capability in (
                    "direct_chat",
                    "image_generation",
                    "image_understanding",
                    "video_understanding",
                    "product_search",
                    "price_compare",
                    "render_3d",
                )
            },
        ),
        notes=[sanitize_error_message(note) for note in extra_notes or []],
    )


def redact_provider_diagnostic_payload(payload: Any) -> Any:
    """Redact arbitrary diagnostic payloads before logging or API return."""

    return sanitize_error_detail(payload)
