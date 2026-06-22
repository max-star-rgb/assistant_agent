import io
import json
import urllib.error

import pytest

from multimodal_agent.providers import qwen_image_generation as qwen_image
from multimodal_agent.providers.qwen_image_generation import (
    QwenImageGenerationAdapter,
    QwenImageGenerationConfig,
    build_qwen_image_payload,
    normalize_qwen_image_size,
    parse_qwen_image_urls,
    qwen_image_generation_url,
)
from multimodal_agent.services.image_generation_adapter import ImageGenerationInput
from multimodal_agent.services.provider_errors import ProviderAdapterError


def test_build_qwen_image_payload_matches_dashscope_shape() -> None:
    payload = build_qwen_image_payload(prompt="生成一张白色运动鞋主图", seed=123)

    assert payload["model"] == "qwen-image-2.0-pro"
    assert payload["input"]["messages"][0]["role"] == "user"
    assert payload["input"]["messages"][0]["content"] == [{"text": "生成一张白色运动鞋主图"}]
    assert payload["parameters"]["size"] == "1024*1024"
    assert payload["parameters"]["n"] == 1
    assert payload["parameters"]["prompt_extend"] is True
    assert payload["parameters"]["watermark"] is False
    assert payload["parameters"]["negative_prompt"]
    assert payload["parameters"]["seed"] == 123


def test_qwen_image_generation_url_accepts_base_url_or_endpoint() -> None:
    endpoint = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"

    assert qwen_image_generation_url("https://dashscope.aliyuncs.com/api/v1") == endpoint
    assert qwen_image_generation_url(endpoint) == endpoint


def test_normalize_qwen_image_size_accepts_common_llm_formats() -> None:
    assert normalize_qwen_image_size("1024x1024") == "1024*1024"
    assert normalize_qwen_image_size(" 2048X2048 ") == "2048*2048"
    assert normalize_qwen_image_size("2048*2048") == "2048*2048"
    assert normalize_qwen_image_size(None) == "1024*1024"
    assert normalize_qwen_image_size("1024x1024", width=768, height=1024) == "768*1024"


def test_parse_qwen_image_urls_reads_output_choices_content_images() -> None:
    data = {
        "output": {
            "choices": [
                {"message": {"content": [{"image": "https://example.com/a.png"}, {"text": "done"}]}},
                {"message": {"content": [{"image": "https://example.com/b.png"}]}},
            ]
        }
    }

    assert parse_qwen_image_urls(data) == ["https://example.com/a.png", "https://example.com/b.png"]


def test_qwen_adapter_sets_authorization_header_and_parses_images(monkeypatch) -> None:
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "request_id": "req_123",
                    "output": {
                        "choices": [
                            {"message": {"content": [{"image": "https://example.com/generated.png"}]}}
                        ]
                    },
                }
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(qwen_image.urllib.request, "urlopen", fake_urlopen)

    adapter = QwenImageGenerationAdapter(
        QwenImageGenerationConfig(api_key="test-dashscope-key", timeout_seconds=3.0)
    )
    result = adapter.generate(ImageGenerationInput(prompt="生成一张白色运动鞋主图"))

    assert captured["url"].endswith("/services/aigc/multimodal-generation/generation")
    assert captured["headers"]["Authorization"] == "Bearer test-dashscope-key"
    assert captured["payload"]["model"] == "qwen-image-2.0-pro"
    assert captured["payload"]["parameters"]["size"] == "1024*1024"
    assert captured["timeout"] == 3.0
    assert result.image_urls == ["https://example.com/generated.png"]
    assert result.image_url == "https://example.com/generated.png"
    assert result.request_id == "req_123"
    assert result.raw["request_id"] == "req_123"


def test_qwen_adapter_uses_config_default_size_when_input_size_is_unset(monkeypatch) -> None:
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "request_id": "req_size",
                    "output": {
                        "choices": [
                            {"message": {"content": [{"image": "https://example.com/generated.png"}]}}
                        ]
                    },
                }
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr(qwen_image.urllib.request, "urlopen", fake_urlopen)

    adapter = QwenImageGenerationAdapter(
        QwenImageGenerationConfig(api_key="test-dashscope-key", default_size="256*256")
    )
    adapter.generate(ImageGenerationInput(prompt="生成一张白色运动鞋主图"))

    assert captured["payload"]["parameters"]["size"] == "256*256"


def test_qwen_adapter_missing_key_does_not_call_provider(monkeypatch) -> None:
    def fail_urlopen(*args, **kwargs):
        raise AssertionError("missing key must not call DashScope")

    monkeypatch.setattr(qwen_image.urllib.request, "urlopen", fail_urlopen)

    adapter = QwenImageGenerationAdapter(QwenImageGenerationConfig(api_key=None))

    with pytest.raises(ProviderAdapterError) as exc_info:
        adapter.generate(ImageGenerationInput(prompt="生成一张图"))

    assert exc_info.value.code == "provider_unconfigured"
    assert "QWEN_IMAGE_API_KEY" in exc_info.value.message


@pytest.mark.parametrize("status", [400, 401, 500])
def test_qwen_adapter_maps_http_errors_without_leaking_key(monkeypatch, status: int) -> None:
    body = json.dumps({"code": "InvalidApiKey", "message": "bad key", "request_id": "req_bad"}).encode("utf-8")

    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(
            url=request.full_url,
            code=status,
            msg="bad",
            hdrs={},
            fp=io.BytesIO(body),
        )

    monkeypatch.setattr(qwen_image.urllib.request, "urlopen", fake_urlopen)
    adapter = QwenImageGenerationAdapter(QwenImageGenerationConfig(api_key="dashscope-secret-key"))

    with pytest.raises(ProviderAdapterError) as exc_info:
        adapter.generate(ImageGenerationInput(prompt="生成一张图"))

    assert exc_info.value.code.startswith("provider_")
    assert f"status={status}" in exc_info.value.message
    assert "InvalidApiKey" in exc_info.value.message
    assert "req_bad" in exc_info.value.message
    assert "dashscope-secret-key" not in exc_info.value.message
    assert "Bearer" not in exc_info.value.message


def test_qwen_adapter_maps_code_message_response_to_provider_error(monkeypatch) -> None:
    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"code": "BadRequest", "message": "invalid prompt", "request_id": "req_456"}).encode(
                "utf-8"
            )

    monkeypatch.setattr(qwen_image.urllib.request, "urlopen", lambda request, timeout: FakeResponse())
    adapter = QwenImageGenerationAdapter(QwenImageGenerationConfig(api_key="test-key"))

    with pytest.raises(ProviderAdapterError) as exc_info:
        adapter.generate(ImageGenerationInput(prompt="生成一张图"))

    assert exc_info.value.code == "provider_execution_failed"
    assert "BadRequest" in exc_info.value.message
    assert "req_456" in exc_info.value.message
