from multimodal_agent.services.provider_errors import (
    ProviderAdapterError,
    ProviderError,
    build_provider_error,
    map_exception_to_provider_error,
    normalize_provider_error_code,
)


def test_provider_error_schema_normalizes_common_codes() -> None:
    error = build_provider_error(
        "missing_api_key",
        "openai provider is missing OPENAI_API_KEY",
        provider="openai",
        capability="direct_chat",
    )

    assert isinstance(error, ProviderError)
    assert error.code == "provider_missing_api_key"
    assert error.provider == "openai"
    assert error.capability == "direct_chat"
    assert error.recoverable is True


def test_provider_error_taxonomy_contains_required_codes() -> None:
    assert normalize_provider_error_code("provider_unconfigured") == "provider_unconfigured"
    assert normalize_provider_error_code("timeout") == "provider_timeout"
    assert normalize_provider_error_code("bad_response") == "provider_bad_response"
    assert normalize_provider_error_code("auth_failed") == "provider_auth_failed"


def test_exception_mapping_does_not_expose_raw_traceback() -> None:
    try:
        raise RuntimeError("Traceback (most recent call last):\nsecret=abc\nboom")
    except RuntimeError as exc:
        error = map_exception_to_provider_error(exc, provider="mock", capability="image_generation")

    assert error.code == "provider_execution_failed"
    assert "Traceback" not in error.message
    assert "abc" not in error.message
    assert "[redacted]" in error.message
    assert error.detail["exception_type"] == "RuntimeError"


def test_provider_adapter_error_is_sanitized_at_construction() -> None:
    error = ProviderAdapterError("provider_auth_failed", "Bearer sk-test Authorization=secret")

    assert error.code == "provider_auth_failed"
    assert "sk-test" not in str(error)
    assert "secret" not in str(error)
    assert "[redacted]" in str(error)
