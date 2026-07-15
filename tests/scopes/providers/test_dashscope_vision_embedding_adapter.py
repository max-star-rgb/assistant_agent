import io
import json
import urllib.error
from pathlib import Path

import pytest

from assistant_agent.config import ProviderConfig
from assistant_agent.video_ai.detection import vision_embedding_provider as dashscope_embedding
from assistant_agent.video_ai.detection.semantic_detector import cosine_similarity, semantic_change_score
from assistant_agent.video_ai.detection.vision_embedding_provider import (
    DEFAULT_DASHSCOPE_VISION_EMBEDDING_ENDPOINT,
    DEFAULT_DASHSCOPE_VISION_EMBEDDING_MODEL,
    DashScopeVisionEmbeddingConfig,
    DashScopeVisionEmbeddingProvider,
    build_dashscope_vision_embedding_payload,
    create_vision_embedding_provider,
    dashscope_multimodal_embedding_url,
)
from assistant_agent.video_ai.types import VideoFrame


pytestmark = pytest.mark.fast


def test_build_dashscope_payload_uses_native_multimodal_embedding_shape() -> None:
    payload = build_dashscope_vision_embedding_payload(
        image="https://example.com/frame.jpg",
        model=DEFAULT_DASHSCOPE_VISION_EMBEDDING_MODEL,
        dimension=768,
    )

    assert payload == {
        "model": "tongyi-embedding-vision-flash-2026-03-06",
        "input": {"contents": [{"image": "https://example.com/frame.jpg"}]},
        "parameters": {"dimension": 768},
    }


def test_dashscope_embedding_url_accepts_base_url_or_endpoint() -> None:
    assert dashscope_multimodal_embedding_url("https://dashscope.aliyuncs.com/api/v1") == (
        DEFAULT_DASHSCOPE_VISION_EMBEDDING_ENDPOINT
    )
    assert dashscope_multimodal_embedding_url(DEFAULT_DASHSCOPE_VISION_EMBEDDING_ENDPOINT) == (
        DEFAULT_DASHSCOPE_VISION_EMBEDDING_ENDPOINT
    )


def test_dashscope_adapter_posts_to_direct_http_endpoint_with_bearer_auth(monkeypatch) -> None:
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
                    "request_id": "req_embed_1",
                    "output": {
                        "embeddings": [
                            {"index": 0, "type": "image", "embedding": [0.1, 0.2, 0.3]},
                        ]
                    },
                    "usage": {"input_tokens": 3},
                }
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(dashscope_embedding.urllib.request, "urlopen", fake_urlopen)

    adapter = DashScopeVisionEmbeddingProvider(
        DashScopeVisionEmbeddingConfig(api_key="test-dashscope-key", timeout_seconds=2.5)
    )
    result = adapter.embed(VideoFrame(frame_id="frame-1", timestamp_seconds=0.0, uri="https://example.com/f.jpg"))

    assert captured["url"] == DEFAULT_DASHSCOPE_VISION_EMBEDDING_ENDPOINT
    assert "/compatible-mode/" not in captured["url"]
    assert not captured["url"].rstrip("/").endswith("/embeddings")
    assert captured["headers"]["Authorization"] == "Bearer test-dashscope-key"
    assert captured["payload"]["model"] == DEFAULT_DASHSCOPE_VISION_EMBEDDING_MODEL
    assert captured["payload"]["input"]["contents"] == [{"image": "https://example.com/f.jpg"}]
    assert captured["payload"]["parameters"]["dimension"] == 768
    assert captured["timeout"] == 2.5
    assert result.embedding == [0.1, 0.2, 0.3]
    assert result.provider == "dashscope"
    assert result.request_id == "req_embed_1"
    assert result.errors == []


def test_dashscope_adapter_converts_local_image_to_data_uri(monkeypatch, tmp_path: Path) -> None:
    image_path = tmp_path / "frame.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"output": {"embeddings": [{"embedding": [1.0, 0.0]}]}}).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr(dashscope_embedding.urllib.request, "urlopen", fake_urlopen)

    adapter = DashScopeVisionEmbeddingProvider(DashScopeVisionEmbeddingConfig(api_key="test-key"))
    result = adapter.embed(VideoFrame(frame_id="local", timestamp_seconds=0.0, uri=str(image_path)))

    image = captured["payload"]["input"]["contents"][0]["image"]
    assert image.startswith("data:image/png;base64,")
    assert result.embedding == [1.0, 0.0]


def test_dashscope_adapter_missing_key_returns_structured_error_without_http_call(monkeypatch) -> None:
    def fail_urlopen(*args, **kwargs):
        raise AssertionError("missing key must not call DashScope")

    monkeypatch.setattr(dashscope_embedding.urllib.request, "urlopen", fail_urlopen)

    adapter = DashScopeVisionEmbeddingProvider(DashScopeVisionEmbeddingConfig(api_key=None))
    result = adapter.embed(VideoFrame(frame_id="frame-1", timestamp_seconds=0.0, uri="https://example.com/f.jpg"))

    assert result.embedding == []
    assert result.errors[0]["code"] == "provider_unconfigured"
    assert "DASHSCOPE_API_KEY" in result.errors[0]["message"]


def test_dashscope_adapter_maps_http_errors_without_leaking_key(monkeypatch) -> None:
    body = json.dumps({"code": "InvalidApiKey", "message": "bad key", "request_id": "req_bad"}).encode("utf-8")

    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(
            url=request.full_url,
            code=401,
            msg="bad",
            hdrs={},
            fp=io.BytesIO(body),
        )

    monkeypatch.setattr(dashscope_embedding.urllib.request, "urlopen", fake_urlopen)

    adapter = DashScopeVisionEmbeddingProvider(DashScopeVisionEmbeddingConfig(api_key="dashscope-secret-key"))
    result = adapter.embed(VideoFrame(frame_id="frame-1", timestamp_seconds=0.0, uri="https://example.com/f.jpg"))

    rendered = str(result.errors)
    assert result.embedding == []
    assert result.errors[0]["code"] == "provider_auth_failed"
    assert "req_bad" in result.errors[0]["message"]
    assert "dashscope-secret-key" not in rendered
    assert "Bearer" not in rendered


def test_semantic_change_score_uses_cosine_similarity() -> None:
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert semantic_change_score([1.0, 0.0], [1.0, 0.0]) == pytest.approx(0.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert semantic_change_score([1.0, 0.0], [0.0, 1.0]) == pytest.approx(1.0)


def test_provider_config_selects_dashscope_embedding_only_in_real_provider_profiles() -> None:
    offline = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "offline_eval",
            "MULTIMODAL_AGENT_VISION_EMBEDDING_PROVIDER": "dashscope",
            "DASHSCOPE_API_KEY": "test-key",
        }
    )
    smoke = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_VISION_EMBEDDING_PROVIDER": "dashscope",
            "DASHSCOPE_API_KEY": "test-key",
            "DASHSCOPE_MULTIMODAL_EMBEDDING_BASE_URL": "https://dashscope.local/api/v1",
            "DASHSCOPE_VISION_EMBEDDING_MODEL": "vision-embedding-test",
            "DASHSCOPE_VISION_EMBEDDING_DIMENSION": "512",
        }
    )

    assert offline.vision_embedding_provider == "mock"
    assert isinstance(create_vision_embedding_provider(offline), dashscope_embedding.MockVisionEmbeddingProvider)
    assert smoke.vision_embedding_provider == "dashscope"
    assert smoke.vision_embedding_api_key == "test-key"
    assert smoke.vision_embedding_base_url == "https://dashscope.local/api/v1"
    assert smoke.vision_embedding_model == "vision-embedding-test"
    assert smoke.vision_embedding_dimension == 512
    assert isinstance(create_vision_embedding_provider(smoke), DashScopeVisionEmbeddingProvider)


def test_provider_config_uses_qwen_vision_key_as_dashscope_embedding_fallback() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_VISION_EMBEDDING_PROVIDER": "dashscope",
            "QWEN_VISION_API_KEY": "test-qwen-vision-key",
        }
    )

    assert config.vision_embedding_provider == "dashscope"
    assert config.vision_embedding_api_key == "test-qwen-vision-key"
