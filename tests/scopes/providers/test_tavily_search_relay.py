import io
import json
import urllib.error

import pytest

from scripts.run_tavily_search_relay import (
    RelayConfig,
    build_tavily_extract_payload,
    build_tavily_payload,
    handle_fetch_request,
    handle_relay_request,
)


class FakeResponse:
    def __init__(self, payload: object | bytes) -> None:
        self.payload = payload

    def read(self) -> bytes:
        if isinstance(self.payload, bytes):
            return self.payload
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None


def _config() -> RelayConfig:
    return RelayConfig(
        relay_api_key="relay-client-secret",
        tavily_api_key="tvly-secret",
        tavily_base_url="https://api.tavily.test/search",
        search_depth="basic",
        timeout_seconds=3.5,
    )


def test_tavily_relay_config_requires_relay_and_tavily_keys() -> None:
    with pytest.raises(ValueError, match="WEB_SEARCH_RELAY_API_KEY"):
        RelayConfig.from_env({"TAVILY_API_KEY": "tvly-secret"})


def test_tavily_relay_converts_request_and_normalizes_results() -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout):  # noqa: ANN001
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(request.header_items())
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse(
            {
                "query": "python news",
                "answer": "Python release news summary.",
                "results": [
                    {
                        "title": "Python News",
                        "url": "https://docs.example.com/news",
                        "content": "A concise result snippet.",
                        "published_date": "2026-07-15",
                    }
                ],
                "request_id": "tvly-request-1",
            }
        )

    status, payload = handle_relay_request(
        json.dumps(
            {
                "query": " python news ",
                "limit": 50,
                "recency_days": 7,
                "site_filter": "https://docs.example.com/path",
            }
        ).encode("utf-8"),
        {"Authorization": "Bearer relay-client-secret"},
        _config(),
        opener=fake_urlopen,
    )

    assert status == 200
    assert captured["url"] == "https://api.tavily.test/search"
    assert captured["timeout"] == 3.5
    assert captured["headers"]["Authorization"] == "Bearer tvly-secret"
    assert captured["payload"] == {
        "query": "python news",
        "max_results": 10,
        "search_depth": "basic",
        "include_answer": True,
        "include_raw_content": False,
        "include_images": False,
        "include_domains": ["docs.example.com"],
        "time_range": "week",
    }
    assert payload == {
        "provider": "tavily",
        "query_used": "python news",
        "results": [
            {
                "title": "Python News",
                "url": "https://docs.example.com/news",
                "snippet": "A concise result snippet.",
                "source": "docs.example.com",
                "published_at": "2026-07-15",
            }
        ],
        "summary": "Python release news summary.",
        "total": 1,
        "output_ref": "tavily://search/tvly-request-1",
    }


@pytest.mark.parametrize(
    ("headers", "expected_status", "expected_code"),
    [
        ({}, 401, "provider_auth_failed"),
        ({"Authorization": "Bearer wrong-secret"}, 403, "provider_permission_denied"),
    ],
)
def test_tavily_relay_rejects_missing_or_wrong_bearer_without_calling_tavily(
    headers: dict[str, str], expected_status: int, expected_code: str
) -> None:
    def fail_urlopen(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("Tavily must not be called for failed relay auth")

    status, payload = handle_relay_request(
        b'{"query": "python"}',
        headers,
        _config(),
        opener=fail_urlopen,
    )

    assert status == expected_status
    assert payload["provider"] == "tavily"
    assert payload["errors"][0]["code"] == expected_code


@pytest.mark.parametrize(
    ("response_factory", "expected_code"),
    [
        (
            lambda: urllib.error.HTTPError(
                "https://api.tavily.test/search",
                429,
                "Too Many Requests",
                {},
                io.BytesIO(b'{"detail":{"error":"rate limited Bearer tvly-secret"}}'),
            ),
            "provider_rate_limited",
        ),
        (
            lambda: TimeoutError("request timed out with Bearer tvly-secret"),
            "provider_timeout",
        ),
        (lambda: FakeResponse(b"{not-json"), "provider_bad_response"),
        (
            lambda: FakeResponse({"query": "python", "results": {"bad": "shape"}}),
            "provider_schema_mismatch",
        ),
    ],
)
def test_tavily_relay_returns_sanitized_error_json(
    response_factory, expected_code: str
) -> None:
    def fake_urlopen(*args, **kwargs):  # noqa: ANN002, ANN003
        response = response_factory()
        if isinstance(response, BaseException):
            raise response
        return response

    status, payload = handle_relay_request(
        b'{"query": "python", "limit": 2}',
        {"Authorization": "Bearer relay-client-secret"},
        _config(),
        opener=fake_urlopen,
    )

    assert status == 200
    assert payload["provider"] == "tavily"
    assert payload["results"] == []
    assert payload["errors"][0]["code"] == expected_code
    assert payload["errors"][0]["message"]
    assert "tvly-secret" not in payload["errors"][0]["message"]


@pytest.mark.parametrize(
    ("recency_days", "expected_time_range"),
    [(1, "day"), (7, "week"), (31, "month"), (365, "year"), (366, None)],
)
def test_tavily_payload_maps_limit_site_filter_and_recency(
    recency_days: int, expected_time_range: str | None
) -> None:
    payload = build_tavily_payload(
        {
            "query": " python ",
            "limit": 0,
            "site_filter": "site:example.com/docs",
            "recency_days": recency_days,
        },
        _config(),
    )

    assert payload["query"] == "python"
    assert payload["max_results"] == 1
    assert payload["include_domains"] == ["example.com"]
    if expected_time_range is None:
        assert "time_range" not in payload
    else:
        assert payload["time_range"] == expected_time_range


def test_tavily_fetch_relay_converts_request_and_normalizes_content() -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout):  # noqa: ANN001
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(request.header_items())
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse(
            {
                "results": [
                    {
                        "url": "https://docs.example.com/news",
                        "title": "Python News",
                        "raw_content": "A readable markdown page body.",
                    }
                ],
                "failed_results": [],
                "request_id": "tvly-extract-1",
            }
        )

    status, payload = handle_fetch_request(
        json.dumps(
            {
                "url": " https://docs.example.com/news ",
                "max_chars": 12,
                "content_format": "markdown",
            }
        ).encode("utf-8"),
        {"Authorization": "Bearer relay-client-secret"},
        _config(),
        opener=fake_urlopen,
    )

    assert status == 200
    assert captured["url"] == "https://api.tavily.com/extract"
    assert captured["timeout"] == 3.5
    assert captured["headers"]["Authorization"] == "Bearer tvly-secret"
    assert captured["payload"] == {
        "urls": ["https://docs.example.com/news"],
        "extract_depth": "basic",
        "include_images": False,
        "format": "markdown",
        "timeout": 3,
    }
    assert payload == {
        "provider": "tavily",
        "url": "https://docs.example.com/news",
        "title": "Python News",
        "content": "A readable m",
        "content_format": "markdown",
        "total_chars": 30,
        "truncated": True,
        "output_ref": "tavily://extract/tvly-extract-1",
    }


@pytest.mark.parametrize(
    ("headers", "expected_status", "expected_code"),
    [
        ({}, 401, "provider_auth_failed"),
        ({"Authorization": "Bearer wrong-secret"}, 403, "provider_permission_denied"),
    ],
)
def test_tavily_fetch_relay_rejects_failed_auth_without_calling_tavily(
    headers: dict[str, str], expected_status: int, expected_code: str
) -> None:
    def fail_urlopen(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("Tavily must not be called for failed relay auth")

    status, payload = handle_fetch_request(
        b'{"url": "https://docs.example.com/news"}',
        headers,
        _config(),
        opener=fail_urlopen,
    )

    assert status == expected_status
    assert payload["provider"] == "tavily"
    assert payload["url"] == "web_fetch"
    assert payload["errors"][0]["code"] == expected_code


@pytest.mark.parametrize(
    ("response_factory", "expected_code"),
    [
        (
            lambda: urllib.error.HTTPError(
                "https://api.tavily.com/extract",
                429,
                "Too Many Requests",
                {},
                io.BytesIO(b'{"detail":{"error":"rate limited Bearer tvly-secret"}}'),
            ),
            "provider_rate_limited",
        ),
        (lambda: TimeoutError("extract timed out with Bearer tvly-secret"), "provider_timeout"),
        (lambda: FakeResponse(b"{not-json"), "provider_bad_response"),
        (lambda: FakeResponse({"results": {"bad": "shape"}}), "provider_schema_mismatch"),
    ],
)
def test_tavily_fetch_relay_returns_sanitized_error_json(
    response_factory, expected_code: str
) -> None:
    def fake_urlopen(*args, **kwargs):  # noqa: ANN002, ANN003
        response = response_factory()
        if isinstance(response, BaseException):
            raise response
        return response

    status, payload = handle_fetch_request(
        b'{"url": "https://docs.example.com/news"}',
        {"Authorization": "Bearer relay-client-secret"},
        _config(),
        opener=fake_urlopen,
    )

    assert status == 200
    assert payload["provider"] == "tavily"
    assert payload["content"] == ""
    assert payload["errors"][0]["code"] == expected_code
    assert payload["errors"][0]["message"]
    assert "tvly-secret" not in payload["errors"][0]["message"]


def test_tavily_extract_payload_maps_format_and_url() -> None:
    payload = build_tavily_extract_payload(
        {
            "url": " https://example.com/article ",
            "content_format": "text",
        },
        _config(),
    )

    assert payload["urls"] == ["https://example.com/article"]
    assert payload["extract_depth"] == "basic"
    assert payload["format"] == "text"
