from multimodal_agent.config import ProviderConfig
from multimodal_agent.services.chat_adapter import ChatRequest, create_chat_adapter
from multimodal_agent.services.image_generation_adapter import ImageGenerationInput, create_image_generation_adapter
from multimodal_agent.services.provider_errors import ProviderSafetyPolicy, build_provider_error
from multimodal_agent.services.real_vision_adapter import RealVisionProviderConfig
from multimodal_agent.services.provider_selection import create_vision_adapter
from multimodal_agent.services.vision_adapter import VisionUnderstandingInput


def test_provider_unconfigured_uses_structured_safe_error() -> None:
    adapter = create_chat_adapter(ProviderConfig(chat_provider="openai", openai_api_key=None))

    result = adapter.chat(ChatRequest(user_id="u1", session_id="s1", user_query="hello"))

    assert result.errors[0].code == "provider_unconfigured"
    assert result.errors[0].recoverable is True
    assert "OPENAI_API_KEY" in result.errors[0].message
    assert "sk-" not in result.errors[0].message


def test_provider_auth_failed_error_is_sanitized() -> None:
    error = build_provider_error(
        "provider_auth_failed",
        "Authorization: Bearer sk-live-secret-token",
        provider="qwen",
        capability="image_understanding",
    )

    assert error.code == "provider_auth_failed"
    assert error.recoverable is False
    assert "Authorization" not in error.message
    assert "sk-live-secret-token" not in error.message
    assert "[redacted]" in error.message


def test_provider_timeout_and_bad_response_are_stable_codes() -> None:
    timeout = build_provider_error("provider_timeout", "provider timed out after 10s")
    bad_response = build_provider_error("provider_bad_response", "raw provider payload was invalid")

    assert timeout.code == "provider_timeout"
    assert timeout.recoverable is True
    assert bad_response.code == "provider_bad_response"
    assert bad_response.recoverable is False


def test_adapter_result_errors_are_policy_sanitized() -> None:
    adapter = create_image_generation_adapter(
        ProviderConfig(image_generation_provider="openai", openai_api_key=None)
    )

    result = adapter.generate(ImageGenerationInput(prompt="画一张海报"))

    assert result.errors[0]["code"] == "provider_unconfigured"
    assert result.error is not None
    assert "sk-" not in result.error
    assert "Bearer" not in result.error


def test_policy_removes_raw_provider_detail_fields() -> None:
    policy = ProviderSafetyPolicy()
    detail = policy.sanitize_detail(
        {
            "raw_response": {"Authorization": "Bearer sk-secret", "body": "x" * 1000},
            "raw_provider_payload": {"api_key": "sk-test", "body": "raw"},
            "media": {"image_base64": "data:image/png;base64," + ("A" * 200), "output_ref": "artifact://image/1"},
            "status_code": 500,
        }
    )

    assert "raw_response" not in detail
    assert "raw_provider_payload" not in detail
    assert "image_base64" not in detail["media"]
    assert detail["media"]["output_ref"] == "artifact://image/1"
    assert detail["status_code"] == 500


def test_real_vision_missing_key_does_not_fallback_to_mock_or_leak_secret() -> None:
    adapter = create_vision_adapter(
        ProviderConfig(
            vision_provider="qwen",
            qwen_api_key=None,
            qwen_vision_base_url="https://dashscope.example.test/compatible-mode/v1",
            qwen_vision_model="qwen-vl",
        )
    )

    assert isinstance(adapter.config, RealVisionProviderConfig)
    try:
        adapter.understand(VisionUnderstandingInput(image_ids=["image1"]))
    except Exception as exc:
        message = str(exc)
    else:  # pragma: no cover - adapter must fail without key
        raise AssertionError("expected provider_unconfigured")

    assert "provider_unconfigured" in message
    assert "mock://vision" not in message
    assert "Bearer" not in message
