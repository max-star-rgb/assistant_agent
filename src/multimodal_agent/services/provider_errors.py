"""Provider adapter error taxonomy and safety helpers."""

from __future__ import annotations

import re
import traceback
from typing import Any

from pydantic import BaseModel, Field


class ProviderAdapterError(ValueError):
    """Error raised by optional real provider adapters."""

    def __init__(self, code: str, message: str) -> None:
        self.code = normalize_provider_error_code(code)
        sanitized = sanitize_error_message(message)
        self.message = sanitized
        super().__init__(f"{self.code}: {sanitized}")


class ProviderError(BaseModel):
    """Stable provider error shape shared by adapters, API, trace, and evals."""

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    detail: dict[str, Any] = Field(default_factory=dict)
    recoverable: bool = False
    provider: str | None = None
    capability: str | None = None


class ProviderSafetyPolicy(BaseModel):
    """Policy for provider-facing error sanitization."""

    max_message_chars: int = Field(default=300, ge=32)
    max_detail_chars: int = Field(default=500, ge=64)
    redaction_token: str = "[redacted]"
    redact_absolute_paths: bool = True
    redact_base64: bool = True
    redact_tracebacks: bool = True

    def sanitize_message(self, value: object) -> str:
        """Return a compact provider-safe message."""

        text = _stringify(value)
        if self.redact_tracebacks:
            text = _strip_traceback(text)
        text = " ".join(text.strip().split())
        text = _redact_secrets(text, self.redaction_token)
        if self.redact_base64:
            text = _redact_base64(text, self.redaction_token)
        if self.redact_absolute_paths:
            text = _redact_absolute_paths(text, self.redaction_token)
        return _truncate(text or "provider error", self.max_message_chars)

    def sanitize_detail(self, value: Any) -> Any:
        """Recursively sanitize provider error detail."""

        return _sanitize_detail_value(value, self)

    def build_error(
        self,
        code: str,
        message: object,
        *,
        detail: dict[str, Any] | None = None,
        recoverable: bool | None = None,
        provider: str | None = None,
        capability: str | None = None,
    ) -> ProviderError:
        """Create a normalized ProviderError."""

        normalized = normalize_provider_error_code(code)
        return ProviderError(
            code=normalized,
            message=self.sanitize_message(message),
            detail=self.sanitize_detail(detail or {}),
            recoverable=is_recoverable_provider_error(normalized) if recoverable is None else recoverable,
            provider=provider,
            capability=capability,
        )


PROVIDER_ERROR_CODES = frozenset(
    {
        "provider_unconfigured",
        "provider_missing_api_key",
        "provider_missing_base_url",
        "provider_invalid_config",
        "provider_request_invalid",
        "provider_request_too_large",
        "provider_unsupported_input",
        "provider_unsupported_format",
        "provider_timeout",
        "provider_network_error",
        "provider_unavailable",
        "provider_bad_gateway",
        "provider_auth_failed",
        "provider_permission_denied",
        "provider_rate_limited",
        "provider_bad_response",
        "provider_empty_response",
        "provider_schema_mismatch",
        "provider_execution_failed",
        "provider_cancelled",
        "provider_unknown_error",
        "provider_budget_exceeded",
        "provider_call_limit_exceeded",
        "provider_input_size_exceeded",
    }
)

PROVIDER_RECOVERABLE_CODES = frozenset(
    {
        "provider_unconfigured",
        "provider_missing_api_key",
        "provider_missing_base_url",
        "provider_timeout",
        "provider_network_error",
        "provider_unavailable",
        "provider_bad_gateway",
        "provider_rate_limited",
        "provider_execution_failed",
    }
)

_DEFAULT_POLICY = ProviderSafetyPolicy()
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|apikey|authorization|bearer|cookie|secret|token|password)\b\s*[:=]\s*([^\s,;]+)"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_KEY_PREFIX_RE = re.compile(r"\b(?:sk|pk|qwen|dashscope)-[A-Za-z0-9._-]{4,}\b", flags=re.IGNORECASE)
_BASE64_RE = re.compile(r"\b(?:[A-Za-z0-9+/]{80,}={0,2}|data:[^;\s]+;base64,[A-Za-z0-9+/=]{32,})\b")
_ABSOLUTE_PATH_RE = re.compile(r"(?<!\w)/(?:home|Users|tmp|var|mnt|media|workspace)/[^\s,;:\"]+")


def normalize_provider_error_code(code: str | None) -> str:
    """Normalize provider adapter error codes to the shared taxonomy."""

    normalized = (code or "").strip().lower()
    aliases = {
        "timeout": "provider_timeout",
        "timed_out": "provider_timeout",
        "rate_limited": "provider_rate_limited",
        "auth_failed": "provider_auth_failed",
        "unauthorized": "provider_auth_failed",
        "forbidden": "provider_permission_denied",
        "bad_response": "provider_bad_response",
        "invalid_response": "provider_bad_response",
        "unavailable": "provider_unavailable",
        "network_error": "provider_network_error",
        "missing_api_key": "provider_missing_api_key",
        "missing_base_url": "provider_missing_base_url",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized in PROVIDER_ERROR_CODES:
        return normalized
    if normalized.startswith("provider_"):
        return "provider_unknown_error"
    return normalized or "provider_unknown_error"


def is_recoverable_provider_error(code: str) -> bool:
    """Return whether a provider error is usually recoverable."""

    return normalize_provider_error_code(code) in PROVIDER_RECOVERABLE_CODES


def sanitize_error_message(value: object, policy: ProviderSafetyPolicy | None = None) -> str:
    """Remove secrets, tracebacks, base64, private paths, and raw response bulk."""

    return (policy or _DEFAULT_POLICY).sanitize_message(value)


def sanitize_error_detail(value: Any, policy: ProviderSafetyPolicy | None = None) -> Any:
    """Recursively sanitize provider error details."""

    return (policy or _DEFAULT_POLICY).sanitize_detail(value)


def build_provider_error(
    code: str,
    message: object,
    *,
    detail: dict[str, Any] | None = None,
    recoverable: bool | None = None,
    provider: str | None = None,
    capability: str | None = None,
    policy: ProviderSafetyPolicy | None = None,
) -> ProviderError:
    """Create a sanitized provider error."""

    return (policy or _DEFAULT_POLICY).build_error(
        code,
        message,
        detail=detail,
        recoverable=recoverable,
        provider=provider,
        capability=capability,
    )


def map_exception_to_provider_error(
    exc: BaseException,
    *,
    provider: str | None = None,
    capability: str | None = None,
    code: str | None = None,
    policy: ProviderSafetyPolicy | None = None,
) -> ProviderError:
    """Map internal exceptions to a stable, sanitized provider error."""

    if isinstance(exc, ProviderAdapterError):
        mapped_code = code or exc.code
    elif isinstance(exc, TimeoutError):
        mapped_code = code or "provider_timeout"
    elif isinstance(exc, PermissionError):
        mapped_code = code or "provider_permission_denied"
    elif isinstance(exc, (ConnectionError, OSError)):
        mapped_code = code or "provider_network_error"
    elif isinstance(exc, (KeyError, TypeError, ValueError)):
        mapped_code = code or "provider_bad_response"
    else:
        mapped_code = code or "provider_execution_failed"

    return build_provider_error(
        mapped_code,
        str(exc),
        detail={"exception_type": exc.__class__.__name__},
        provider=provider,
        capability=capability,
        policy=policy,
    )


def _sanitize_detail_value(value: Any, policy: ProviderSafetyPolicy) -> Any:
    if isinstance(value, str):
        return policy.sanitize_message(value)
    if isinstance(value, dict):
        return {
            policy.sanitize_message(key): _sanitize_detail_value(child, policy)
            for key, child in value.items()
            if not _is_raw_provider_key(str(key))
        }
    if isinstance(value, list):
        return [_sanitize_detail_value(item, policy) for item in value[:20]]
    if value is None or isinstance(value, bool | int | float):
        return value
    return _truncate(policy.sanitize_message(value), policy.max_detail_chars)


def _is_raw_provider_key(key: str) -> bool:
    lowered = key.lower()
    return lowered in {
        "raw",
        "raw_response",
        "provider_raw_response",
        "request_body",
        "headers",
        "authorization",
        "api_key",
        "apikey",
        "bearer",
        "cookie",
        "secret",
        "token",
        "password",
    }


def _stringify(value: object) -> str:
    if isinstance(value, BaseException):
        return "".join(traceback.format_exception_only(type(value), value)).strip()
    return str(value)


def _strip_traceback(text: str) -> str:
    if "Traceback (most recent call last)" not in text:
        return text
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    suffix = f" {_DEFAULT_POLICY.redaction_token}" if _has_secret_marker(text) else ""
    return f"{lines[-1]}{suffix}" if lines else "provider error"


def _redact_secrets(text: str, token: str) -> str:
    text = _BEARER_RE.sub(token, text)
    text = _SECRET_ASSIGNMENT_RE.sub(token, text)
    text = _KEY_PREFIX_RE.sub(token, text)
    words = []
    redact_next = False
    secret_markers = {"authorization", "bearer", "cookie", "secret", "token", "password", "api_key", "apikey"}
    for word in text.split(" "):
        lowered = word.strip(":=").lower()
        if redact_next:
            words.append(token)
            redact_next = False
        elif lowered in secret_markers:
            words.append(token)
            redact_next = True
        else:
            words.append(word)
    return " ".join(words)


def _redact_base64(text: str, token: str) -> str:
    return _BASE64_RE.sub(token, text)


def _redact_absolute_paths(text: str, token: str) -> str:
    return _ABSOLUTE_PATH_RE.sub(token, text)


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 3]}..."


def _has_secret_marker(text: str) -> bool:
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in ("api_key", "apikey", "authorization", "bearer", "cookie", "secret", "token", "password")
    )
