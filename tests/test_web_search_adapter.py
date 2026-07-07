from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.web_search import WebSearchRequest
from assistant_agent.services.web_search_adapter import (
    HttpWebSearchAdapter,
    MockWebSearchAdapter,
    create_web_search_adapter,
)


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
