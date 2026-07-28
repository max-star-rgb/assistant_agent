from __future__ import annotations

import json
from typing import Any

from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.tools.plugins.builtin.web_access.fetch_models import WebFetchRequest
from assistant_agent.tools.plugins.builtin.web_access.search_models import WebSearchRequest
from assistant_agent.tools.plugins.builtin.web_access.fetch_backend import (
    TavilyWebFetchAdapter,
    create_web_fetch_adapter,
)
from assistant_agent.tools.plugins.builtin.web_access.fetch_tool import WebFetchTool
from assistant_agent.tools.plugins.builtin.web_access.search_backend import (
    TavilyWebSearchAdapter,
    create_web_search_adapter,
)
from assistant_agent.tools.plugins.builtin.web_access.search_tool import WebSearchTool
from assistant_agent.tools.plugins.builtin.web_access.plugin import web_provider_ready


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def test_tavily_search_adapter_maps_request_and_normalizes_response(
    monkeypatch,
) -> None:
    captured: dict[str, Any] = {}

    def urlopen(request, *, timeout: float):
        captured["url"] = request.full_url
        captured["authorization"] = request.headers["Authorization"]
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _FakeResponse(
            {
                "query": "latest release",
                "answer": "A concise answer.",
                "request_id": "search-request",
                "results": [
                    {
                        "title": "Release notes",
                        "url": "https://example.com/release",
                        "content": "Release details.",
                        "published_date": "2026-07-22",
                    }
                ],
            }
        )

    monkeypatch.setattr(
        "assistant_agent.tools.plugins.builtin.web_access."
        "search_backend.urllib.request.urlopen",
        urlopen,
    )
    adapter = TavilyWebSearchAdapter(
        base_url="https://api.tavily.test",
        api_key="test-key",
        timeout_seconds=3.0,
    )

    result = adapter.search(
        WebSearchRequest(
            query="latest release",
            recency_days=7,
            site_filter="example.com",
            limit=3,
        )
    )

    assert captured["url"] == "https://api.tavily.test/search"
    assert captured["authorization"] == "Bearer test-key"
    assert captured["timeout"] == 3.0
    assert captured["payload"]["query"] == "latest release"
    assert captured["payload"]["max_results"] == 3
    assert captured["payload"]["search_depth"] == "basic"
    assert captured["payload"]["include_domains"] == ["example.com"]
    assert "start_date" in captured["payload"]
    assert result.success is True
    assert result.provider == "tavily"
    assert result.summary == "A concise answer."
    assert result.results[0].snippet == "Release details."
    assert result.results[0].published_at == "2026-07-22"
    assert result.output_ref == "tavily://search/search-request"


def test_tavily_search_adapter_treats_empty_results_as_successful_outcome(
    monkeypatch,
) -> None:
    def urlopen(_request, *, timeout: float):
        assert timeout == 3.0
        return _FakeResponse(
            {
                "query": "no matching result",
                "request_id": "empty-search-request",
                "results": [],
            }
        )

    monkeypatch.setattr(
        "assistant_agent.tools.plugins.builtin.web_access."
        "search_backend.urllib.request.urlopen",
        urlopen,
    )
    adapter = TavilyWebSearchAdapter(
        base_url="https://api.tavily.test",
        api_key="test-key",
        timeout_seconds=3.0,
    )
    result = adapter.search(WebSearchRequest(query="no matching result"))

    assert result.success is True
    assert result.outcome == "empty"
    assert result.results == []
    assert result.errors == []
    assert result.output_ref == "tavily://search/empty-search-request"
    tool_result = WebSearchTool(adapter=adapter).run(
        WebSearchRequest(query="no matching result")
    )
    assert tool_result.success is True
    assert tool_result.data is not None
    assert tool_result.data["outcome"] == "empty"
    assert tool_result.contract is not None
    assert tool_result.contract.status == "succeeded"


def test_tavily_fetch_adapter_maps_extract_and_normalizes_response(
    monkeypatch,
) -> None:
    captured: dict[str, Any] = {}

    def urlopen(request, *, timeout: float):
        captured["url"] = request.full_url
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _FakeResponse(
            {
                "request_id": "extract-request",
                "results": [
                    {
                        "url": "https://example.com/article",
                        "raw_content": "# Article\n\nReadable content.",
                    }
                ],
                "failed_results": [],
            }
        )

    monkeypatch.setattr(
        "assistant_agent.tools.plugins.builtin.web_access."
        "fetch_backend.urllib.request.urlopen",
        urlopen,
    )
    adapter = TavilyWebFetchAdapter(
        base_url="https://api.tavily.test",
        api_key="test-key",
        timeout_seconds=4.0,
    )

    result = adapter.fetch(
        WebFetchRequest(
            url="https://example.com/article",
            max_chars=200,
            content_format="markdown",
        )
    )

    assert captured == {
        "url": "https://api.tavily.test/extract",
        "payload": {
            "urls": "https://example.com/article",
            "extract_depth": "basic",
            "format": "markdown",
        },
        "timeout": 4.0,
    }
    assert result.success is True
    assert result.provider == "tavily"
    assert result.content == "# Article\n\nReadable content."
    assert result.output_ref == "tavily://extract/extract-request"


def test_tavily_fetch_adapter_preserves_failed_result_detail(
    monkeypatch,
) -> None:
    def urlopen(_request, *, timeout: float):
        assert timeout == 4.0
        return _FakeResponse(
            {
                "request_id": "failed-extract-request",
                "results": [],
                "failed_results": [
                    {
                        "url": "https://example.com/missing",
                        "error": "HTTP 404: page not found",
                    }
                ],
            }
        )

    monkeypatch.setattr(
        "assistant_agent.tools.plugins.builtin.web_access."
        "fetch_backend.urllib.request.urlopen",
        urlopen,
    )
    result = TavilyWebFetchAdapter(
        base_url="https://api.tavily.test",
        api_key="test-key",
        timeout_seconds=4.0,
    ).fetch(WebFetchRequest(url="https://example.com/missing"))

    assert result.success is False
    assert result.outcome == "failed"
    assert result.errors[0].code == "provider_request_invalid"
    assert result.errors[0].message == "HTTP 404: page not found"
    assert result.errors[0].recoverable is False
    assert result.output_ref == "tavily://extract/failed-extract-request"


def test_tavily_fetch_adapter_treats_clean_empty_response_as_empty_outcome(
    monkeypatch,
) -> None:
    def urlopen(_request, *, timeout: float):
        assert timeout == 4.0
        return _FakeResponse(
            {
                "request_id": "empty-extract-request",
                "results": [],
                "failed_results": [],
            }
        )

    monkeypatch.setattr(
        "assistant_agent.tools.plugins.builtin.web_access."
        "fetch_backend.urllib.request.urlopen",
        urlopen,
    )
    adapter = TavilyWebFetchAdapter(
        base_url="https://api.tavily.test",
        api_key="test-key",
        timeout_seconds=4.0,
    )
    result = adapter.fetch(WebFetchRequest(url="https://example.com/empty"))

    assert result.success is True
    assert result.outcome == "empty"
    assert result.content == ""
    assert result.errors == []
    assert result.output_ref == "tavily://extract/empty-extract-request"
    tool_result = WebFetchTool(adapter=adapter).run(
        WebFetchRequest(url="https://example.com/empty")
    )
    assert tool_result.success is True
    assert tool_result.data is not None
    assert tool_result.data["outcome"] == "empty"
    assert tool_result.contract is not None
    assert tool_result.contract.status == "succeeded"


def test_tavily_provider_is_built_in_process_when_explicitly_configured() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_PROVIDER_MODE": "real",
            "MULTIMODAL_AGENT_CHAT_PROVIDER": "qwen",
            "QWEN_API_KEY": "test-chat-key",
            "MULTIMODAL_AGENT_SEARCH_PROVIDER": "tavily",
            "TAVILY_API_KEY": "test-tavily-key",
        }
    )

    assert config.search_provider == "tavily"
    assert web_provider_ready(config) is True
    assert isinstance(create_web_search_adapter(config), TavilyWebSearchAdapter)
    assert isinstance(create_web_fetch_adapter(config), TavilyWebFetchAdapter)
    runtime = AgentGraphRuntime(config=config)
    assert {"web_search", "web_fetch"}.issubset(runtime.registry.list())
