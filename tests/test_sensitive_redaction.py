from multimodal_agent.agent.recovery import sanitize_error_message as sanitize_recovery_error_message
from multimodal_agent.schemas.api import api_error
from multimodal_agent.services.provider_errors import sanitize_error_detail, sanitize_error_message
from multimodal_agent.services.trace_store import sanitize_trace_value


def test_sensitive_tokens_are_redacted_everywhere() -> None:
    raw = "provider_timeout: Authorization: Bearer sk-test api_key=abc secret=hidden password=hunter2"

    sanitized = sanitize_error_message(raw)

    assert "sk-test" not in sanitized
    assert "api_key=abc" not in sanitized
    assert "hidden" not in sanitized
    assert "hunter2" not in sanitized
    assert "[redacted]" in sanitized


def test_full_base64_and_private_paths_are_redacted() -> None:
    raw_base64 = "a" * 120
    raw = f"provider_bad_response: image=data:image/png;base64,{raw_base64} path=/home/user/private/image.png"

    sanitized = sanitize_error_message(raw)

    assert raw_base64 not in sanitized
    assert "/home/user/private/image.png" not in sanitized
    assert "[redacted]" in sanitized


def test_raw_traceback_is_not_exposed() -> None:
    raw = (
        "Traceback (most recent call last):\n"
        '  File "/home/user/project/provider.py", line 1, in call\n'
        "RuntimeError: Authorization=Bearer sk-test boom"
    )

    sanitized = sanitize_error_message(raw)

    assert "Traceback" not in sanitized
    assert "/home/user/project/provider.py" not in sanitized
    assert "sk-test" not in sanitized
    assert "[redacted]" in sanitized


def test_api_and_trace_use_same_redaction_policy() -> None:
    raw = "provider_timeout: bearer sk-test token=abc timed out"

    api = api_error("provider_timeout", raw)
    trace_message = sanitize_trace_value(raw)
    recovery_message = sanitize_recovery_error_message(raw)

    for value in (api.message, trace_message, recovery_message):
        assert "sk-test" not in value
        assert "token=abc" not in value
        assert "[redacted]" in value


def test_sensitive_detail_is_sanitized_recursively() -> None:
    detail = sanitize_error_detail(
        {
            "headers": {"Authorization": "Bearer sk-test"},
            "nested": {"path": "/home/user/private/file.txt", "token": "abc123"},
        }
    )

    assert "headers" not in detail
    dumped = str(detail)
    assert "sk-test" not in dumped
    assert "/home/user/private/file.txt" not in dumped
    assert "abc123" not in dumped
    assert "[redacted]" in dumped
