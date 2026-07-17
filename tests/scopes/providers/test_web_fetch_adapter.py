import json

from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.web_fetch import WebFetchRequest
from assistant_agent.services.web_fetch_adapter import (
    HttpWebFetchAdapter,
    MockWebFetchAdapter,
    create_web_fetch_adapter,
)


class _FakeResponse:
    def __init__(self, payload: str) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return self.payload.encode("utf-8")

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None


def test_mock_web_fetch_adapter_returns_stable_structured_content() -> None:
    result = MockWebFetchAdapter().fetch(
        WebFetchRequest(url="https://example.com/article", max_chars=80)
    )

    assert result.success is True
    assert result.provider == "mock"
    assert result.url == "https://example.com/article"
    assert result.title == "Mock page for https://example.com/article"
    assert result.content
    assert result.content_format == "markdown"
    assert result.total_chars >= len(result.content)
    assert result.output_ref == "mock://web_fetch/example-com-article"
    assert result.errors == []


def test_http_web_fetch_adapter_missing_config_returns_provider_unconfigured() -> None:
    result = HttpWebFetchAdapter(
        base_url=None,
        api_key=None,
    ).fetch(WebFetchRequest(url="https://example.com/article"))

    assert result.success is False
    assert result.provider == "http"
    assert result.errors[0].code == "provider_unconfigured"
    assert "WEB_SEARCH_BASE_URL" in result.errors[0].message
    assert "WEB_SEARCH_API_KEY" in result.errors[0].message


def test_http_web_fetch_adapter_posts_to_sibling_fetch_endpoint(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout):  # noqa: ANN001
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(request.header_items())
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(
            """
            {
              "provider": "tavily",
              "url": "https://example.com/article",
              "title": "Example Article",
              "content": "Readable page content.",
              "content_format": "markdown",
              "total_chars": 22,
              "truncated": false,
              "output_ref": "tavily://extract/request-1"
            }
            """
        )

    monkeypatch.setattr(
        "assistant_agent.services.web_fetch_adapter.urllib.request.urlopen",
        fake_urlopen,
    )

    result = HttpWebFetchAdapter(
        base_url="https://relay.local/search",
        api_key="relay-client-secret",
        timeout_seconds=4.5,
    ).fetch(WebFetchRequest(url="https://example.com/article", max_chars=200))

    assert captured["url"] == "https://relay.local/fetch"
    assert captured["timeout"] == 4.5
    assert captured["headers"]["Authorization"] == "Bearer relay-client-secret"
    assert captured["payload"] == {
        "url": "https://example.com/article",
        "max_chars": 200,
        "content_format": "markdown",
    }
    assert result.success is True
    assert result.provider == "tavily"
    assert result.title == "Example Article"
    assert result.content == "Readable page content."
    assert result.output_ref == "tavily://extract/request-1"


def test_http_web_fetch_adapter_loopback_base_url_bypasses_global_proxy_urlopen(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeOpener:
        def open(self, request, timeout):  # noqa: ANN001
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            return _FakeResponse(
                """
                {
                  "provider": "tavily",
                  "url": "https://example.com/article",
                  "title": "Example Article",
                  "content": "Readable page content.",
                  "content_format": "markdown",
                  "total_chars": 22,
                  "truncated": false
                }
                """
            )

    def fake_urlopen(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("loopback web_fetch requests must bypass global proxy urlopen")

    def fake_proxy_handler(proxies):  # noqa: ANN001
        captured["proxy_handler"] = dict(proxies)
        return ("proxy_handler", dict(proxies))

    def fake_build_opener(handler):  # noqa: ANN001
        captured["opener_handler"] = handler
        return FakeOpener()

    monkeypatch.setattr(
        "assistant_agent.services.web_fetch_adapter.urllib.request.urlopen",
        fake_urlopen,
    )
    monkeypatch.setattr(
        "assistant_agent.services.web_fetch_adapter.urllib.request.ProxyHandler",
        fake_proxy_handler,
    )
    monkeypatch.setattr(
        "assistant_agent.services.web_fetch_adapter.urllib.request.build_opener",
        fake_build_opener,
    )

    result = HttpWebFetchAdapter(
        base_url="http://127.0.0.1:7005/search",
        api_key="relay-client-secret",
        timeout_seconds=0.5,
    ).fetch(WebFetchRequest(url="https://example.com/article"))

    assert result.success is True
    assert captured["proxy_handler"] == {}
    assert captured["opener_handler"] == ("proxy_handler", {})
    assert captured["url"] == "http://127.0.0.1:7005/fetch"
    assert captured["timeout"] == 0.5


def test_http_web_fetch_adapter_preserves_backend_error_payload(monkeypatch) -> None:
    def fake_urlopen(request, timeout):  # noqa: ANN001, ARG001
        return _FakeResponse(
            """
            {
              "provider": "tavily",
              "url": "https://example.com/missing",
              "content": "",
              "content_format": "markdown",
              "total_chars": 0,
              "truncated": false,
              "errors": [
                {
                  "code": "provider_timeout",
                  "message": "tavily extract timed out",
                  "recoverable": true
                }
              ]
            }
            """
        )

    monkeypatch.setattr(
        "assistant_agent.services.web_fetch_adapter.urllib.request.urlopen",
        fake_urlopen,
    )

    result = HttpWebFetchAdapter(
        base_url="https://relay.local/search",
        api_key="relay-client-secret",
    ).fetch(WebFetchRequest(url="https://example.com/missing"))

    assert result.success is False
    assert result.provider == "tavily"
    assert result.url == "https://example.com/missing"
    assert result.total_chars == 0
    assert result.errors[0].code == "provider_timeout"
    assert result.errors[0].message == "tavily extract timed out"
    assert result.errors[0].recoverable is True


def test_provider_smoke_profile_selects_http_web_fetch_adapter() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_SEARCH_PROVIDER": "http",
            "WEB_SEARCH_BASE_URL": "https://search.local/v1/search",
            "WEB_SEARCH_API_KEY": "sk-web-search-test",
        }
    )

    adapter = create_web_fetch_adapter(config)

    assert isinstance(adapter, HttpWebFetchAdapter)


def test_local_demo_profile_does_not_select_http_web_fetch_from_keys() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_SEARCH_PROVIDER": "http",
            "WEB_SEARCH_BASE_URL": "https://search.local/v1/search",
            "WEB_SEARCH_API_KEY": "sk-web-search-test",
        }
    )

    assert config.search_provider == "mock"
    assert isinstance(create_web_fetch_adapter(config), MockWebFetchAdapter)
