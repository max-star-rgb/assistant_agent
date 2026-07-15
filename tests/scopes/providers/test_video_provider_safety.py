import socket

from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.perception import VideoUnderstandingRequest
from assistant_agent.services.video_adapter import HttpVideoUnderstandingAdapter, create_video_understanding_adapter
from assistant_agent.tools.video_tool import VideoUnderstandingTool


def test_http_video_provider_missing_base_url_returns_provider_unconfigured() -> None:
    adapter = create_video_understanding_adapter(ProviderConfig(video_provider="http"))

    result = adapter.understand_video(VideoUnderstandingRequest(video_ref="mock://video/demo"))

    assert result.provider == "http"
    assert result.errors[0]["code"] == "provider_unconfigured"
    assert "VIDEO_UNDERSTANDING_BASE_URL" in result.errors[0]["message"]


def test_http_video_provider_missing_api_key_returns_provider_unconfigured() -> None:
    adapter = create_video_understanding_adapter(
        ProviderConfig(video_provider="http", video_understanding_base_url="http://video.local")
    )

    result = adapter.understand_video(VideoUnderstandingRequest(video_ref="mock://video/demo"))

    assert result.provider == "http"
    assert result.errors[0]["code"] == "provider_unconfigured"
    assert "VIDEO_UNDERSTANDING_API_KEY" in result.errors[0]["message"]


def test_http_video_provider_large_video_returns_structured_error() -> None:
    adapter = create_video_understanding_adapter(
        ProviderConfig(
            video_provider="http",
            video_understanding_base_url="http://video.local",
            video_understanding_api_key="secret-key",
            max_video_bytes=100,
        )
    )

    result = adapter.understand_video(
        VideoUnderstandingRequest(video_ref="mock://video/large", metadata={"size_bytes": 101})
    )

    assert result.errors[0]["code"] == "video_file_too_large"
    assert "MULTIMODAL_AGENT_MAX_VIDEO_BYTES" in result.errors[0]["message"]


def test_http_video_provider_long_video_returns_structured_error() -> None:
    adapter = create_video_understanding_adapter(
        ProviderConfig(
            video_provider="http",
            video_understanding_base_url="http://video.local",
            video_understanding_api_key="secret-key",
            max_video_seconds=1.0,
        )
    )

    result = adapter.understand_video(
        VideoUnderstandingRequest(video_ref="mock://video/long", metadata={"duration_seconds": 2.0})
    )

    assert result.errors[0]["code"] == "video_file_too_large"
    assert "MULTIMODAL_AGENT_MAX_VIDEO_SECONDS" in result.errors[0]["message"]


def test_http_video_provider_timeout_config_is_stored_without_network_client() -> None:
    adapter = create_video_understanding_adapter(
        ProviderConfig(video_provider="http", video_understanding_timeout_seconds=3.25)
    )

    assert isinstance(adapter, HttpVideoUnderstandingAdapter)
    assert adapter.timeout_seconds == 3.25


def test_http_video_provider_skeleton_does_not_call_network(monkeypatch) -> None:
    def fail_network_call(*args: object, **kwargs: object) -> None:
        raise AssertionError("network should not be called")

    monkeypatch.setattr(socket, "create_connection", fail_network_call)
    adapter = create_video_understanding_adapter(
        ProviderConfig(
            video_provider="http",
            video_understanding_base_url="http://video.local",
            video_understanding_api_key="secret-key",
        )
    )

    result = adapter.understand_video(VideoUnderstandingRequest(video_ref="mock://video/demo"))

    assert result.errors[0]["code"] == "video_provider_unavailable"


def test_video_tool_redacts_secret_provider_fields_from_contract() -> None:
    adapter = create_video_understanding_adapter(
        ProviderConfig(
            video_provider="http",
            video_understanding_base_url="http://video.local",
            video_understanding_api_key="secret-video-key",
        )
    )

    result = VideoUnderstandingTool(adapter=adapter).run({"video_ref": "mock://video/demo"})
    payload = result.model_dump(mode="json")

    assert result.success is False
    assert "secret-video-key" not in str(payload)
    assert "Authorization" not in str(payload)
    assert "Bearer" not in str(payload)
    assert "base64" not in str(payload)
