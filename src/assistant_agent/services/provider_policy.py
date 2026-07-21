"""Provider timeout, retry, and fallback policies."""

from __future__ import annotations

import os
from collections.abc import Mapping

from pydantic import BaseModel, Field

from assistant_agent.services.provider_errors import normalize_provider_error_code
from assistant_agent.schemas.tool_ids import (
    DIRECT_CHAT_CAPABILITY,
    IMAGE_GENERATION_CAPABILITY,
    IMAGE_UNDERSTANDING_CAPABILITY,
    IMAGE_UNDERSTANDING_TOOL_NAME,
    SHOPPING_SEARCH_CAPABILITY,
    VIDEO_UNDERSTANDING_CAPABILITY,
)


DEFAULT_RETRYABLE_PROVIDER_ERRORS = (
    "provider_timeout",
    "provider_network_error",
    "provider_rate_limited",
    "provider_unavailable",
)

NON_RETRYABLE_PROVIDER_ERRORS = frozenset(
    {
        "provider_unconfigured",
        "provider_missing_api_key",
        "provider_missing_base_url",
        "provider_auth_failed",
        "provider_permission_denied",
        "provider_request_invalid",
        "provider_request_too_large",
        "provider_unsupported_input",
        "provider_unsupported_format",
        "provider_bad_response",
        "provider_schema_mismatch",
    }
)


class TimeoutPolicy(BaseModel):
    """Configurable provider timeout defaults.

    This task defines the policy object; individual real adapters may consume
    capability-specific values when their explicit integration is added.
    """

    default_provider_timeout_seconds: float = Field(default=30.0, gt=0)
    chat_timeout_seconds: float = Field(default=30.0, gt=0)
    image_timeout_seconds: float = Field(default=60.0, gt=0)
    vision_timeout_seconds: float = Field(default=60.0, gt=0)
    video_timeout_seconds: float = Field(default=120.0, gt=0)
    search_timeout_seconds: float = Field(default=20.0, gt=0)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "TimeoutPolicy":
        source = os.environ if env is None else env
        return cls(
            default_provider_timeout_seconds=_float_env(
                source.get("MULTIMODAL_AGENT_DEFAULT_PROVIDER_TIMEOUT_SECONDS"),
                30.0,
            ),
            chat_timeout_seconds=_float_env(source.get("MULTIMODAL_AGENT_CHAT_TIMEOUT_SECONDS"), 30.0),
            image_timeout_seconds=_float_env(source.get("MULTIMODAL_AGENT_IMAGE_TIMEOUT_SECONDS"), 60.0),
            vision_timeout_seconds=_float_env(source.get("MULTIMODAL_AGENT_VISION_TIMEOUT_SECONDS"), 60.0),
            video_timeout_seconds=_float_env(source.get("MULTIMODAL_AGENT_VIDEO_TIMEOUT_SECONDS"), 120.0),
            search_timeout_seconds=_float_env(source.get("MULTIMODAL_AGENT_SEARCH_TIMEOUT_SECONDS"), 20.0),
        )

    def for_capability(self, capability: str | None) -> float:
        """Return timeout seconds for a capability name."""

        mapping = {
            DIRECT_CHAT_CAPABILITY: self.chat_timeout_seconds,
            IMAGE_GENERATION_CAPABILITY: self.image_timeout_seconds,
            IMAGE_UNDERSTANDING_CAPABILITY: self.vision_timeout_seconds,
            IMAGE_UNDERSTANDING_TOOL_NAME: self.vision_timeout_seconds,
            VIDEO_UNDERSTANDING_CAPABILITY: self.video_timeout_seconds,
            SHOPPING_SEARCH_CAPABILITY: self.search_timeout_seconds,
        }
        return mapping.get(capability or "", self.default_provider_timeout_seconds)


class RetryPolicy(BaseModel):
    """Provider retry policy with explicit non-retryable guardrails."""

    max_retries: int = Field(default=1, ge=0)
    backoff_seconds: float = Field(default=0.0, ge=0.0)
    retry_on: tuple[str, ...] = DEFAULT_RETRYABLE_PROVIDER_ERRORS

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "RetryPolicy":
        source = os.environ if env is None else env
        retry_on = _csv_env(source.get("MULTIMODAL_AGENT_PROVIDER_RETRY_ON"))
        return cls(
            max_retries=_int_env(source.get("MULTIMODAL_AGENT_PROVIDER_MAX_RETRIES"), 1),
            backoff_seconds=_float_env(source.get("MULTIMODAL_AGENT_PROVIDER_RETRY_BACKOFF_SECONDS"), 0.0),
            retry_on=tuple(retry_on) if retry_on else DEFAULT_RETRYABLE_PROVIDER_ERRORS,
        )

    def is_retryable(self, code: str) -> bool:
        """Return whether the code is eligible for retry."""

        normalized = normalize_provider_error_code(code)
        if normalized in NON_RETRYABLE_PROVIDER_ERRORS:
            return False
        return normalized in {normalize_provider_error_code(item) for item in self.retry_on}

    def should_retry(self, code: str, failed_attempts: int) -> bool:
        """Return whether another attempt is allowed after failed_attempts."""

        return failed_attempts <= self.max_retries and self.is_retryable(code)


class FallbackPolicy(BaseModel):
    """Provider partial-result policy; cross-mode fallback is forbidden."""

    allow_partial_result: bool = True
    fallback_providers: dict[str, list[str]] = Field(default_factory=dict)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "FallbackPolicy":
        source = os.environ if env is None else env
        return cls(
            allow_partial_result=_bool_env(source.get("MULTIMODAL_AGENT_ALLOW_PARTIAL_RESULT"), default=True),
            fallback_providers={},
        )


class ProviderExecutionPolicy(BaseModel):
    """Combined provider execution policy used by tool execution."""

    timeout: TimeoutPolicy = Field(default_factory=TimeoutPolicy)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    fallback: FallbackPolicy = Field(default_factory=FallbackPolicy)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ProviderExecutionPolicy":
        return cls(
            timeout=TimeoutPolicy.from_env(env),
            retry=RetryPolicy.from_env(env),
            fallback=FallbackPolicy.from_env(env),
        )


def _float_env(value: str | None, default: float) -> float:
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _int_env(value: str | None, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed >= 0 else default


def _bool_env(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _csv_env(value: str | None) -> list[str]:
    if value is None:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]
