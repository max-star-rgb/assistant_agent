from assistant_agent.config import ProviderConfig
from assistant_agent.services.provider_readiness import (
    build_provider_readiness_report,
    build_smoke_contract,
)


def test_default_readiness_is_offline_ready_without_real_provider_calls() -> None:
    report = build_provider_readiness_report(ProviderConfig.from_env({}))

    assert report.runtime_profile == "local_demo"
    assert report.ready is True
    assert {check.provider for check in report.checks} == {"mock"}
    assert all(check.status == "ready" for check in report.checks)


def test_provider_smoke_missing_config_is_not_ready() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_VISION_PROVIDER": "qwen",
        }
    )

    report = build_provider_readiness_report(config)
    vision = next(check for check in report.checks if check.capability == "image_understanding")

    assert report.ready is False
    assert vision.provider == "qwen"
    assert vision.status == "not_ready"
    assert vision.issues[0].missing == ["QWEN_API_KEY"]


def test_provider_smoke_configured_qwen_vision_is_ready_without_calling_provider() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_VISION_PROVIDER": "qwen",
            "QWEN_VISION_API_KEY": "test-qwen-key",
        }
    )

    report = build_provider_readiness_report(config)
    vision = next(check for check in report.checks if check.capability == "image_understanding")

    assert report.ready is True
    assert vision.provider == "qwen"
    assert vision.status == "ready"
    assert vision.real_provider_allowed is True


def test_provider_smoke_missing_qwen_vision_marks_video_understanding_not_ready() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_VISION_PROVIDER": "qwen",
        }
    )

    report = build_provider_readiness_report(config)
    video = next(check for check in report.checks if check.capability == "video_understanding")

    assert video.provider == "qwen"
    assert video.status == "not_ready"
    assert video.issues[0].missing == ["QWEN_API_KEY"]


def test_provider_smoke_configured_qwen_vision_marks_video_understanding_ready() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_VISION_PROVIDER": "qwen",
            "QWEN_VISION_API_KEY": "test-qwen-video-key",
        }
    )

    report = build_provider_readiness_report(config)
    video = next(check for check in report.checks if check.capability == "video_understanding")

    assert video.provider == "qwen"
    assert video.status == "ready"
    assert video.real_provider_allowed is True


def test_smoke_contract_marks_real_provider_disabled_under_local_demo() -> None:
    config = ProviderConfig.from_env({})

    contract = build_smoke_contract(
        config=config,
        capability="image_understanding",
        provider="qwen",
        success=False,
    )

    assert contract.status == "skipped"
    assert contract.runtime_profile == "local_demo"
    assert contract.readiness == "disabled"
    assert contract.errors == []


def test_smoke_contract_failed_shape_is_stable_and_structured() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_IMAGE_PROVIDER": "openai",
        }
    )

    contract = build_smoke_contract(
        config=config,
        capability="image_generation",
        provider="openai",
        success=False,
        errors=[{"code": "provider_unconfigured", "message": "missing OPENAI_API_KEY"}],
    )

    payload = contract.model_dump(mode="json")
    assert payload["status"] == "failed"
    assert payload["provider"] == "openai"
    assert payload["capability"] == "image_generation"
    assert payload["runtime_profile"] == "provider_smoke"
    assert payload["readiness"] == "not_ready"
    assert payload["errors"][0]["code"] == "provider_unconfigured"


def test_provider_smoke_missing_ark_chat_model_is_not_ready() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_CHAT_PROVIDER": "ark",
            "ARK_CHAT_API_KEY": "test-ark-key",
        }
    )

    report = build_provider_readiness_report(config)
    chat = next(check for check in report.checks if check.capability == "direct_chat")

    assert report.ready is False
    assert chat.provider == "ark"
    assert chat.status == "not_ready"
    assert chat.issues[0].missing == ["ARK_CHAT_MODEL"]
