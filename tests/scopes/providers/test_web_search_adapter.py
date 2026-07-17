from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.web_search import WebSearchRequest
from assistant_agent.services.web_search_adapter import (
    HttpWebSearchAdapter,
    MockWebSearchAdapter,
    create_web_search_adapter,
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


def test_mock_web_search_adapter_returns_stable_structured_results() -> None:
    result = MockWebSearchAdapter().search(
        WebSearchRequest(query="OpenAI latest news", limit=2)
    )

    assert result.success is True
    assert result.provider == "mock"
    assert result.query_used == "OpenAI latest news"
    assert result.total == 2
    assert result.output_ref == "mock://web_search/openai-latest-news"
    assert result.results[0].title
    assert result.results[0].url.startswith("mock://web-search/")
    assert result.errors == []


def test_http_web_search_adapter_missing_config_returns_provider_unconfigured() -> None:
    result = HttpWebSearchAdapter(
        base_url=None,
        api_key=None,
    ).search(WebSearchRequest(query="OpenAI latest news"))

    assert result.success is False
    assert result.provider == "http"
    assert result.errors[0].code == "provider_unconfigured"
    assert "WEB_SEARCH_BASE_URL" in result.errors[0].message
    assert "WEB_SEARCH_API_KEY" in result.errors[0].message


def test_http_web_search_adapter_preserves_backend_error_payload(monkeypatch) -> None:
    def fake_urlopen(request, timeout):  # noqa: ANN001, ARG001
        return _FakeResponse(
            """
            {
              "provider": "tavily",
              "query_used": "OpenAI latest news",
              "results": [],
              "total": 0,
              "errors": [
                {
                  "code": "provider_timeout",
                  "message": "tavily search timed out",
                  "recoverable": true
                }
              ]
            }
            """
        )

    monkeypatch.setattr(
        "assistant_agent.services.web_search_adapter.urllib.request.urlopen",
        fake_urlopen,
    )

    result = HttpWebSearchAdapter(
        base_url="https://search.local/v1/search",
        api_key="relay-client-secret",
    ).search(WebSearchRequest(query="OpenAI latest news"))

    assert result.success is False
    assert result.provider == "tavily"
    assert result.query_used == "OpenAI latest news"
    assert result.total == 0
    assert result.errors[0].code == "provider_timeout"
    assert result.errors[0].message == "tavily search timed out"
    assert result.errors[0].recoverable is True


def test_http_web_search_adapter_loopback_base_url_bypasses_global_proxy_urlopen(
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
                  "query_used": "python news",
                  "results": [
                    {
                      "title": "Python News",
                      "url": "https://example.com/python",
                      "snippet": "Python update."
                    }
                  ],
                  "total": 1
                }
                """
            )

    def fake_urlopen(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("loopback web_search requests must bypass global proxy urlopen")

    def fake_proxy_handler(proxies):  # noqa: ANN001
        captured["proxy_handler"] = dict(proxies)
        return ("proxy_handler", dict(proxies))

    def fake_build_opener(handler):  # noqa: ANN001
        captured["opener_handler"] = handler
        return FakeOpener()

    monkeypatch.setattr(
        "assistant_agent.services.web_search_adapter.urllib.request.urlopen",
        fake_urlopen,
    )
    monkeypatch.setattr(
        "assistant_agent.services.web_search_adapter.urllib.request.ProxyHandler",
        fake_proxy_handler,
    )
    monkeypatch.setattr(
        "assistant_agent.services.web_search_adapter.urllib.request.build_opener",
        fake_build_opener,
    )

    result = HttpWebSearchAdapter(
        base_url="http://127.0.0.1:7005/search",
        api_key="relay-client-secret",
        timeout_seconds=0.5,
    ).search(WebSearchRequest(query="python news", limit=1))

    assert result.success is True
    assert captured["proxy_handler"] == {}
    assert captured["opener_handler"] == ("proxy_handler", {})
    assert captured["url"] == "http://127.0.0.1:7005/search"
    assert captured["timeout"] == 0.5


def test_local_demo_profile_does_not_select_http_web_search_from_keys() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_SEARCH_PROVIDER": "http",
            "WEB_SEARCH_BASE_URL": "https://search.local/v1/search",
            "WEB_SEARCH_API_KEY": "sk-web-search-test",
        }
    )

    assert config.runtime_profile.name == "local_demo"
    assert config.search_provider == "mock"
    assert isinstance(create_web_search_adapter(config), MockWebSearchAdapter)


def test_provider_smoke_profile_explicitly_selects_http_web_search() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_SEARCH_PROVIDER": "http",
            "WEB_SEARCH_BASE_URL": "https://search.local/v1/search",
            "WEB_SEARCH_API_KEY": "sk-web-search-test",
            "WEB_SEARCH_TIMEOUT_SECONDS": "4.5",
        }
    )

    adapter = create_web_search_adapter(config)

    assert config.search_provider == "http"
    assert config.web_search_base_url == "https://search.local/v1/search"
    assert config.web_search_api_key == "sk-web-search-test"
    assert config.web_search_timeout_seconds == 4.5
    assert isinstance(adapter, HttpWebSearchAdapter)
