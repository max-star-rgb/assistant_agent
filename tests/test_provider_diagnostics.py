from multimodal_agent.config import ProviderConfig
from multimodal_agent.services.provider_diagnostics import (
    build_provider_diagnostics_summary,
    redact_provider_diagnostic_payload,
)
from multimodal_agent.services.provider_policy import ProviderExecutionPolicy


def test_provider_diagnostics_default_summary_is_offline_and_safe() -> None:
    summary = build_provider_diagnostics_summary(ProviderConfig.from_env({}))

    assert summary.runtime_profile == "local_demo"
    assert summary.allows_real_providers is False
    assert summary.validation_valid is True
    assert summary.validation_issue_count == 0
    assert {item.provider for item in summary.selected_providers} == {"mock"}
    assert summary.safety_defaults.allow_mock_fallback is False
    assert summary.safety_defaults.max_retries == 1
    assert summary.safety_defaults.timeout_seconds_by_capability["video_understanding"] == 120.0


def test_provider_diagnostics_redacts_notes_and_excludes_secret_values() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_VISION_PROVIDER": "qwen",
            "QWEN_API_KEY": "sk-diagnostic-secret",
        }
    )

    summary = build_provider_diagnostics_summary(
        config,
        extra_notes=["Authorization: Bearer sk-diagnostic-secret from /home/user/private/file.jpg"],
    )
    rendered = str(summary.model_dump(mode="json"))

    assert summary.runtime_profile == "provider_smoke"
    assert summary.allows_real_providers is True
    assert "sk-diagnostic-secret" not in rendered
    assert "Bearer" not in rendered
    assert "/home/user" not in rendered
    assert "[redacted]" in rendered


def test_provider_diagnostics_uses_execution_policy_overrides() -> None:
    policy = ProviderExecutionPolicy.from_env(
        {
            "MULTIMODAL_AGENT_PROVIDER_MAX_RETRIES": "2",
            "MULTIMODAL_AGENT_CHAT_TIMEOUT_SECONDS": "9",
        }
    )

    summary = build_provider_diagnostics_summary(ProviderConfig.from_env({}), execution_policy=policy)

    assert summary.safety_defaults.max_retries == 2
    assert summary.safety_defaults.timeout_seconds_by_capability["direct_chat"] == 9.0
    assert summary.safety_defaults.allow_mock_fallback is False


def test_redact_provider_diagnostic_payload_removes_raw_sensitive_fields() -> None:
    payload = {
        "raw_response": {"body": "x" * 1000},
        "headers": {"Authorization": "Bearer sk-secret-token"},
        "path": "/home/user/private/image.png",
    }

    redacted = redact_provider_diagnostic_payload(payload)
    rendered = str(redacted)

    assert "raw_response" not in redacted
    assert "sk-secret-token" not in rendered
    assert "Bearer" not in rendered
    assert "/home/user" not in rendered
