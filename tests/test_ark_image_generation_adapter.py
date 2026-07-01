import io
import json
import urllib.error

import pytest

from assistant_agent.providers import ark_image_generation as ark_image
from assistant_agent.providers.ark_image_generation import (
    ArkImageGenerationAdapter,
    ArkImageGenerationConfig,
    ark_image_generation_url,
    build_ark_image_payload,
    normalize_ark_image_size,
    parse_ark_image_urls,
)
from assistant_agent.services.image_generation_adapter import ImageGenerationInput
from assistant_agent.services.provider_errors import ProviderAdapterError


def test_build_ark_image_payload_matches_openai_images_shape() -> None:
    payload = build_ark_image_payload(prompt="生成一张白色运动鞋主图", watermark=False)

    assert payload["model"] == "doubao-seedream-5-0-260128"
    assert payload["prompt"] == "生成一张白色运动鞋主图"
    assert payload["size"] == "2K"
    assert payload["output_format"] == "png"
    assert payload["response_format"] == "url"
    assert payload["extra_body"] == {"watermark": False}


def test_ark_image_generation_url_accepts_base_url_or_endpoint() -> None:
    endpoint = "https://ark.cn-beijing.volces.com/api/v3/images/generations"

    assert ark_image_generation_url("https://ark.cn-beijing.volces.com/api/v3") == endpoint
    assert ark_image_generation_url(endpoint) == endpoint


def test_parse_ark_image_urls_reads_openai_data_urls() -> None:
    assert parse_ark_image_urls({"data": [{"url": "https://example.com/a.png"}, {"url": "https://example.com/b.png"}]}) == [
        "https://example.com/a.png",
        "https://example.com/b.png",
    ]


def test_normalize_ark_image_size_accepts_common_llm_formats() -> None:
    assert normalize_ark_image_size("2K") == "2K"
    assert normalize_ark_image_size("2k") == "2K"
    assert normalize_ark_image_size("1024x1024") == "2K"
    assert normalize_ark_image_size("1024*1024") == "2K"
    assert normalize_ark_image_size("2048*2048") == "2K"
    assert normalize_ark_image_size(None) == "2K"
    assert normalize_ark_image_size("1024x1024", width=1024, height=1024) == "2K"


def test_ark_adapter_sets_authorization_header_and_parses_images(monkeypatch) -> None:
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"id": "req_ark", "data": [{"url": "https://example.com/generated.png"}]}).encode(
                "utf-8"
            )

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(ark_image.urllib.request, "urlopen", fake_urlopen)

    adapter = ArkImageGenerationAdapter(ArkImageGenerationConfig(api_key="test-ark-key", timeout_seconds=3.0))
    result = adapter.generate(ImageGenerationInput(prompt="生成一张白色运动鞋主图"))

    assert captured["url"].endswith("/images/generations")
    assert captured["headers"]["Authorization"] == "Bearer test-ark-key"
    assert captured["payload"]["model"] == "doubao-seedream-5-0-260128"
    assert captured["payload"]["size"] == "2K"
    assert captured["payload"]["response_format"] == "url"
    assert captured["payload"]["extra_body"] == {"watermark": False}
    assert captured["timeout"] == 3.0
    assert result.provider == "ark"
    assert result.image_urls == ["https://example.com/generated.png"]
    assert result.request_id == "req_ark"


def test_ark_adapter_normalizes_llm_pixel_size_to_ark_token(monkeypatch) -> None:
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"id": "req_ark_size", "data": [{"url": "https://example.com/generated.png"}]}).encode(
                "utf-8"
            )

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr(ark_image.urllib.request, "urlopen", fake_urlopen)
    adapter = ArkImageGenerationAdapter(ArkImageGenerationConfig(api_key="test-ark-key"))

    adapter.generate(ImageGenerationInput(prompt="生成蛋糕", size="1024x1024"))

    assert captured["payload"]["size"] == "2K"


def test_ark_adapter_missing_key_does_not_call_provider(monkeypatch) -> None:
    def fail_urlopen(*args, **kwargs):
        raise AssertionError("missing key must not call Ark")

    monkeypatch.setattr(ark_image.urllib.request, "urlopen", fail_urlopen)

    adapter = ArkImageGenerationAdapter(ArkImageGenerationConfig(api_key=None))

    with pytest.raises(ProviderAdapterError) as exc_info:
        adapter.generate(ImageGenerationInput(prompt="生成一张图"))

    assert exc_info.value.code == "provider_unconfigured"
    assert "ARK_IMAGE_API_KEY" in exc_info.value.message


def test_ark_adapter_rejects_non_latin_header_values_without_calling_provider(monkeypatch) -> None:
    def fail_urlopen(*args, **kwargs):
        raise AssertionError("invalid header must not call Ark")

    monkeypatch.setattr(ark_image.urllib.request, "urlopen", fail_urlopen)

    adapter = ArkImageGenerationAdapter(ArkImageGenerationConfig(api_key="“bad-key”"))

    with pytest.raises(ProviderAdapterError) as exc_info:
        adapter.generate(ImageGenerationInput(prompt="生成一张图"))

    assert exc_info.value.code == "provider_invalid_config"
    assert "non-latin-1 characters" in exc_info.value.message
    assert ".env" in exc_info.value.message
    assert "bad-key" not in exc_info.value.message


@pytest.mark.parametrize("status", [400, 401, 500])
def test_ark_adapter_maps_http_errors_without_leaking_key(monkeypatch, status: int) -> None:
    body = json.dumps({"error": {"code": "InvalidApiKey", "message": "bad key"}, "id": "req_bad"}).encode("utf-8")

    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(
            url=request.full_url,
            code=status,
            msg="bad",
            hdrs={},
            fp=io.BytesIO(body),
        )

    monkeypatch.setattr(ark_image.urllib.request, "urlopen", fake_urlopen)
    adapter = ArkImageGenerationAdapter(ArkImageGenerationConfig(api_key="ark-secret-key"))

    with pytest.raises(ProviderAdapterError) as exc_info:
        adapter.generate(ImageGenerationInput(prompt="生成一张图"))

    assert exc_info.value.code.startswith("provider_")
    assert f"status={status}" in exc_info.value.message
    assert "InvalidApiKey" in exc_info.value.message
    assert "req_bad" in exc_info.value.message
    assert "ark-secret-key" not in exc_info.value.message
    assert "Bearer" not in exc_info.value.message
