from assistant_agent.config import ProviderConfig
from assistant_agent.services.provider_config_validation import (
    validate_provider_config,
    validation_issue_to_provider_error,
)


def test_default_provider_config_validation_is_valid_and_offline() -> None:
    result = validate_provider_config(ProviderConfig.from_env({}))

    assert result.valid is True
    assert result.runtime_profile == "local_demo"
    assert result.issues == []


def test_editable_context_config_defaults_closed() -> None:
    config = ProviderConfig.from_env({})

    assert config.editable_context_enabled is False
    assert config.editable_context_root == ".local/context"
    assert config.editable_context_user_id is None


def test_editable_context_config_uses_only_fixed_env_names() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_EDITABLE_CONTEXT_ENABLED": "true",
            "MULTIMODAL_AGENT_EDITABLE_CONTEXT_ROOT": "/tmp/local-context",
            "MULTIMODAL_AGENT_EDITABLE_CONTEXT_USER_ID": "owner-1",
        }
    )

    assert config.editable_context_enabled is True
    assert config.editable_context_root == "/tmp/local-context"
    assert config.editable_context_user_id == "owner-1"


def test_durable_task_config_defaults_closed() -> None:
    config = ProviderConfig.from_env({})

    assert config.durable_tasks_enabled is False
    assert config.durable_task_worker_enabled is False
    assert config.durable_task_path == ".local/tasks/durable_tasks.sqlite3"
    assert config.durable_task_lease_seconds == 30
    assert config.durable_task_poll_seconds == 1.0


def test_durable_task_config_reads_and_bounds_fixed_env_names() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_DURABLE_TASKS_ENABLED": "true",
            "MULTIMODAL_AGENT_DURABLE_TASK_WORKER_ENABLED": "true",
            "MULTIMODAL_AGENT_DURABLE_TASK_PATH": "/tmp/tasks.sqlite3",
            "MULTIMODAL_AGENT_DURABLE_TASK_LEASE_SECONDS": "2",
            "MULTIMODAL_AGENT_DURABLE_TASK_POLL_SECONDS": "0.01",
        }
    )

    assert config.durable_tasks_enabled is True
    assert config.durable_task_worker_enabled is True
    assert config.durable_task_path == "/tmp/tasks.sqlite3"
    assert config.durable_task_lease_seconds == 5
    assert config.durable_task_poll_seconds == 0.1


def test_provider_smoke_validation_reports_missing_explicit_real_config() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_VISION_PROVIDER": "qwen",
            "MULTIMODAL_AGENT_PRODUCT_PROVIDER": "http",
        }
    )

    result = validate_provider_config(config)

    assert result.valid is False
    assert {(issue.capability, issue.provider) for issue in result.issues} == {
        ("image_understanding", "qwen"),
        ("video_understanding", "qwen"),
        ("product_search", "http"),
    }
    missing = {name for issue in result.issues for name in issue.missing}
    assert {"QWEN_VISION_API_KEY", "PRODUCT_SEARCH_BASE_URL", "PRODUCT_SEARCH_API_KEY"}.issubset(missing)


def test_provider_smoke_validation_accepts_explicit_qwen_vision_config() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_VISION_PROVIDER": "qwen",
            "QWEN_VISION_API_KEY": "test-qwen-key",
        }
    )

    result = validate_provider_config(config)

    assert result.valid is True
    assert result.issues == []


def test_provider_smoke_validation_reports_missing_deepseek_chat_key() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_CHAT_PROVIDER": "deepseek",
        }
    )

    result = validate_provider_config(config)

    assert result.valid is False
    assert result.issues[0].capability == "direct_chat"
    assert result.issues[0].provider == "deepseek"
    assert result.issues[0].missing == ["DEEPSEEK_CHAT_API_KEY"]


def test_chat_validation_uses_provider_spec_defaults_for_deepseek() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_CHAT_PROVIDER": "deepseek",
            "DEEPSEEK_CHAT_API_KEY": "test-deepseek-key",
        }
    )

    result = validate_provider_config(config)

    assert result.valid is True
    assert result.issues == []
    assert config.chat_base_url == "https://api.deepseek.com/v1"
    assert config.chat_model == "deepseek-chat"


def test_chat_validation_requires_explicit_ark_model() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_CHAT_PROVIDER": "ark",
            "ARK_CHAT_API_KEY": "test-ark-key",
        }
    )

    result = validate_provider_config(config)

    assert result.valid is False
    assert result.issues[0].capability == "direct_chat"
    assert result.issues[0].provider == "ark"
    assert result.issues[0].missing == ["ARK_CHAT_MODEL"]


def test_seed_validation_requires_explicit_base_url_not_placeholder() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_VISION_PROVIDER": "seed",
            "SEED_API_KEY": "test-seed-key",
        }
    )

    result = validate_provider_config(config)

    assert result.valid is False
    assert result.issues[0].missing == ["SEED_VISION_BASE_URL"]


def test_image_generation_validation_uses_provider_spec_for_qwen() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_IMAGE_PROVIDER": "qwen",
        }
    )

    result = validate_provider_config(config)

    assert result.valid is False
    assert result.issues[0].capability == "image_generation"
    assert result.issues[0].provider == "qwen"
    assert result.issues[0].missing == ["QWEN_IMAGE_API_KEY"]


def test_validation_issue_provider_error_is_redacted() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_IMAGE_PROVIDER": "openai",
        }
    )
    issue = validate_provider_config(config).issues[0]

    error = validation_issue_to_provider_error(issue)

    assert error.code == "provider_unconfigured"
    assert error.provider == "openai"
    assert error.capability == "image_generation"
    assert error.detail["missing"] == ["OPENAI_API_KEY"]
    assert "sk-" not in error.message
    assert "Bearer" not in error.message
