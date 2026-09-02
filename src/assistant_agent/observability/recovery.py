"""Stable error classification for retained observability records."""

from assistant_agent.providers.provider_errors import normalize_provider_error_code


def classify_error(error: str) -> str:
    """Map raw tool error text to a stable recovery error code."""

    normalized = error.strip().lower()
    prefix = normalized.split(":", maxsplit=1)[0]
    if prefix == "page_timeout":
        return "page_timeout"
    provider_code = normalize_provider_error_code(prefix)
    if (
        provider_code.startswith("provider_")
        and provider_code != "provider_unknown_error"
    ):
        return provider_code
    if normalized.startswith("provider_unconfigured:"):
        return "provider_unconfigured"
    if "timeout" in normalized or "timed out" in normalized:
        return "provider_timeout"
    if "rate limit" in normalized or "rate_limited" in normalized:
        return "provider_rate_limited"
    if (
        normalized.startswith("invalid input:")
        or "缺少" in normalized
        or "missing" in normalized
    ):
        return "tool_input_invalid"
    if "not registered" in normalized:
        return "tool_not_found"
    if normalized.startswith("memory_unavailable:"):
        return "memory_unavailable"
    if normalized.startswith("provider_bad_response:"):
        return "provider_bad_response"
    return "unknown_error"
