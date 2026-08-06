"""Prompt-safety projection for successful Tool observations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from assistant_agent.providers.provider_errors import ProviderSafetyPolicy


_TOOL_OBSERVATION_POLICY = ProviderSafetyPolicy(
    max_message_chars=4_000,
    max_detail_chars=4_000,
)
_UNSAFE_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "authorization",
    "base64",
    "binary",
    "blob",
    "bytes",
    "client_secret",
    "cookie",
    "data_uri",
    "data_url",
    "password",
    "provider_payload",
    "provider_raw_payload",
    "provider_raw_response",
    "raw",
    "raw_body",
    "raw_content",
    "raw_data",
    "raw_output",
    "raw_payload",
    "raw_provider_payload",
    "raw_provider_response",
    "raw_response",
    "refresh_token",
    "secret",
    "secret_token",
}


def sanitize_tool_observation_detail(value: Any) -> Any:
    """Redact unsafe Tool output without imposing error-detail list limits."""

    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str) or key.lower() in _UNSAFE_KEYS:
                continue
            sanitized[key] = sanitize_tool_observation_detail(child)
        return sanitized
    if isinstance(value, list):
        return [sanitize_tool_observation_detail(child) for child in value]
    if isinstance(value, tuple):
        return [sanitize_tool_observation_detail(child) for child in value]
    if isinstance(value, str):
        return _TOOL_OBSERVATION_POLICY.sanitize_message(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _TOOL_OBSERVATION_POLICY.sanitize_message(value)
