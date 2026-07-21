"""Web search adapter interfaces and implementations."""

from __future__ import annotations

import json
import ipaddress
import urllib.error
import urllib.parse
import urllib.request
from time import perf_counter
from typing import Any, Protocol

from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.web_search import (
    WebSearchProviderError,
    WebSearchRequest,
    WebSearchResult,
    WebSearchResultItem,
)
from assistant_agent.services.provider_errors import (
    build_provider_error,
    map_exception_to_provider_error,
)
from assistant_agent.schemas.tool_ids import WEB_SEARCH_CAPABILITY


class WebSearchAdapter(Protocol):
    """Adapter contract for web search providers."""

    def search(self, request: WebSearchRequest) -> WebSearchResult:
        """Return structured web search results."""


class MockWebSearchAdapter:
    """Deterministic local adapter for offline tests and demos."""

    provider = "mock"

    def search(self, request: WebSearchRequest) -> WebSearchResult:
        query = request.query.strip()
        if not query:
            return _failed_web_search_result(
                provider=self.provider,
                query_used=request.query or WEB_SEARCH_CAPABILITY,
                code="web_search_query_empty",
                message="web_search requires query.",
                recoverable=True,
            )

        available = [
            WebSearchResultItem(
                title=f"Mock web result 1 for {query}",
                url="mock://web-search/1",
                snippet=f"Stable offline search result for {query}.",
                source="mock-news",
                published_at="2026-07-07T00:00:00Z",
            ),
            WebSearchResultItem(
                title=f"Mock web result 2 for {query}",
                url="mock://web-search/2",
                snippet=f"Second stable offline result for {query}.",
                source="mock-web",
                published_at="2026-07-06T00:00:00Z",
            ),
            WebSearchResultItem(
                title=f"Mock web result 3 for {query}",
                url="mock://web-search/3",
                snippet=f"Additional offline context for {query}.",
                source="mock-archive",
                published_at="2026-07-05T00:00:00Z",
            ),
        ]
        results = available[: request.limit]
        return WebSearchResult(
            query_used=query,
            results=results,
            summary=f"Mock search returned {len(results)} result(s) for {query}.",
            provider=self.provider,
            total=len(results),
            latency_ms=1,
            output_ref=f"mock://web_search/{_slugify(query)}",
        )


class HttpWebSearchAdapter:
    """Generic HTTP web search adapter.

    The backend is expected to accept a JSON POST body with query, recency_days,
    site_filter, and limit, and to return JSON containing results or items.
    """

    provider = "http"

    def __init__(
        self,
        base_url: str | None,
        api_key: str | None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self._opener = _loopback_proxy_bypass_opener(base_url)

    def search(self, request: WebSearchRequest) -> WebSearchResult:
        missing = []
        if not self.base_url:
            missing.append("WEB_SEARCH_BASE_URL")
        if not self.api_key:
            missing.append("WEB_SEARCH_API_KEY")
        if missing:
            return _failed_web_search_result(
                provider=self.provider,
                query_used=request.query,
                code="provider_unconfigured",
                message=f"http web search provider is missing {', '.join(missing)}.",
                recoverable=True,
            )

        started = perf_counter()
        try:
            http_request = urllib.request.Request(
                self.base_url,
                data=json.dumps(_request_payload(request), ensure_ascii=False).encode(
                    "utf-8"
                ),
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
                method="POST",
            )
            response_context = (
                self._opener.open(http_request, timeout=self.timeout_seconds)
                if self._opener is not None
                else urllib.request.urlopen(http_request, timeout=self.timeout_seconds)
            )
            with response_context as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return _failed_web_search_result(
                provider=self.provider,
                query_used=request.query,
                code=_http_error_code(exc.code),
                message=f"http web search provider returned HTTP {exc.code}.",
                recoverable=exc.code in {408, 429, 500, 502, 503, 504},
                latency_ms=_elapsed_ms(started),
            )
        except urllib.error.URLError as exc:
            return _failed_from_exception(
                exc,
                request=request,
                code="provider_network_error",
                latency_ms=_elapsed_ms(started),
            )
        except TimeoutError as exc:
            return _failed_from_exception(
                exc,
                request=request,
                code="provider_timeout",
                latency_ms=_elapsed_ms(started),
            )
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return _failed_from_exception(
                exc,
                request=request,
                code="provider_bad_response",
                latency_ms=_elapsed_ms(started),
            )
        except Exception as exc:  # pragma: no cover - defensive provider boundary
            return _failed_from_exception(
                exc,
                request=request,
                code="provider_unknown_error",
                latency_ms=_elapsed_ms(started),
            )

        return _web_search_result_from_payload(
            payload, request=request, latency_ms=_elapsed_ms(started)
        )


def create_web_search_adapter(config: ProviderConfig | None = None) -> WebSearchAdapter:
    """Create a web search adapter without initializing real provider clients."""

    resolved = config or ProviderConfig.from_env()
    if resolved.search_provider == "http":
        return HttpWebSearchAdapter(
            base_url=resolved.web_search_base_url,
            api_key=resolved.web_search_api_key,
            timeout_seconds=resolved.web_search_timeout_seconds,
        )
    if resolved.provider_mode == "real":
        raise ValueError("real provider mode requires a configured web search provider")
    return MockWebSearchAdapter()


def _request_payload(request: WebSearchRequest) -> dict[str, Any]:
    payload = request.model_dump(mode="json")
    return {
        key: value for key, value in payload.items() if value not in (None, "", [], {})
    }


def _loopback_proxy_bypass_opener(base_url: str | None) -> Any | None:
    if not _should_bypass_proxy_for_base_url(base_url):
        return None
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _should_bypass_proxy_for_base_url(base_url: str | None) -> bool:
    host = urllib.parse.urlparse(base_url or "").hostname
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _web_search_result_from_payload(
    payload: Any, *, request: WebSearchRequest, latency_ms: int
) -> WebSearchResult:
    if not isinstance(payload, dict):
        return _failed_web_search_result(
            provider="http",
            query_used=request.query,
            code="provider_bad_response",
            message="http web search provider returned a non-object JSON response.",
            recoverable=False,
            latency_ms=latency_ms,
        )

    provider = str(payload.get("provider") or "http")
    query_used = str(payload.get("query_used") or payload.get("query") or request.query)
    backend_errors = _errors_from_payload(payload.get("errors"), provider=provider)
    if backend_errors:
        raw_results_for_error = (
            payload.get("results")
            if isinstance(payload.get("results"), list)
            else payload.get("items")
        )
        if not isinstance(raw_results_for_error, list):
            raw_results_for_error = []
        results_for_error = [
            item
            for item in (
                _result_item_from_payload(item) for item in raw_results_for_error
            )
            if item is not None
        ][: request.limit]
        return WebSearchResult(
            query_used=query_used,
            results=results_for_error,
            summary=payload.get("summary")
            if isinstance(payload.get("summary"), str)
            else None,
            provider=provider,
            total=_int_value(payload.get("total"), default=len(results_for_error)),
            latency_ms=latency_ms,
            output_ref=payload.get("output_ref")
            if isinstance(payload.get("output_ref"), str)
            else None,
            errors=backend_errors,
        )

    raw_results = (
        payload.get("results")
        if isinstance(payload.get("results"), list)
        else payload.get("items")
    )
    if not isinstance(raw_results, list):
        return _failed_web_search_result(
            provider=str(payload.get("provider") or "http"),
            query_used=str(payload.get("query_used") or request.query),
            code="provider_schema_mismatch",
            message="http web search provider response must include results or items.",
            recoverable=False,
            latency_ms=latency_ms,
        )

    results = [_result_item_from_payload(item) for item in raw_results]
    results = [item for item in results if item is not None][: request.limit]
    if not results:
        return _failed_web_search_result(
            provider=provider,
            query_used=query_used,
            code="provider_empty_response",
            message="http web search provider returned no usable results.",
            recoverable=True,
            latency_ms=latency_ms,
        )
    return WebSearchResult(
        query_used=query_used,
        results=results,
        summary=payload.get("summary")
        if isinstance(payload.get("summary"), str)
        else None,
        provider=provider,
        total=_int_value(payload.get("total"), default=len(results)),
        latency_ms=latency_ms,
        output_ref=payload.get("output_ref")
        if isinstance(payload.get("output_ref"), str)
        else None,
    )


def _errors_from_payload(payload: Any, *, provider: str) -> list[WebSearchProviderError]:
    if not isinstance(payload, list):
        return []
    errors: list[WebSearchProviderError] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        code = (
            item.get("code")
            if isinstance(item.get("code"), str)
            else "provider_unknown_error"
        )
        message = (
            item.get("message") if isinstance(item.get("message"), str) else code
        )
        recoverable = (
            item.get("recoverable")
            if isinstance(item.get("recoverable"), bool)
            else None
        )
        error = build_provider_error(
            code,
            message,
            recoverable=recoverable,
            provider=provider,
            capability=WEB_SEARCH_CAPABILITY,
        )
        errors.append(
            WebSearchProviderError(
                code=error.code,
                message=error.message,
                recoverable=error.recoverable,
            )
        )
    return errors


def _result_item_from_payload(payload: Any) -> WebSearchResultItem | None:
    if not isinstance(payload, dict):
        return None
    title = payload.get("title") or payload.get("name")
    url = payload.get("url") or payload.get("link")
    if (
        not isinstance(title, str)
        or not title.strip()
        or not isinstance(url, str)
        or not url.strip()
    ):
        return None
    snippet = (
        payload.get("snippet")
        or payload.get("content")
        or payload.get("description")
        or ""
    )
    return WebSearchResultItem(
        title=title.strip(),
        url=url.strip(),
        snippet=snippet.strip() if isinstance(snippet, str) else "",
        source=payload.get("source")
        if isinstance(payload.get("source"), str)
        else None,
        published_at=payload.get("published_at")
        if isinstance(payload.get("published_at"), str)
        else None,
    )


def _failed_from_exception(
    exc: BaseException,
    *,
    request: WebSearchRequest,
    code: str,
    latency_ms: int,
) -> WebSearchResult:
    error = map_exception_to_provider_error(
        exc, provider="http", capability=WEB_SEARCH_CAPABILITY, code=code
    )
    return WebSearchResult(
        query_used=request.query,
        provider="http",
        errors=[
            WebSearchProviderError(
                code=error.code, message=error.message, recoverable=error.recoverable
            )
        ],
        latency_ms=latency_ms,
        total=0,
    )


def _failed_web_search_result(
    *,
    provider: str,
    query_used: str,
    code: str,
    message: str,
    recoverable: bool,
    latency_ms: int | None = None,
) -> WebSearchResult:
    error = build_provider_error(
        code,
        message,
        recoverable=recoverable,
        provider=provider,
        capability=WEB_SEARCH_CAPABILITY,
    )
    return WebSearchResult(
        query_used=query_used or WEB_SEARCH_CAPABILITY,
        provider=provider,
        errors=[
            WebSearchProviderError(
                code=error.code, message=error.message, recoverable=error.recoverable
            )
        ],
        latency_ms=latency_ms,
        total=0,
    )


def _http_error_code(status_code: int) -> str:
    if status_code in {401, 403}:
        return (
            "provider_auth_failed"
            if status_code == 401
            else "provider_permission_denied"
        )
    if status_code == 429:
        return "provider_rate_limited"
    if status_code == 408:
        return "provider_timeout"
    if status_code in {502, 503, 504}:
        return "provider_unavailable"
    if 500 <= status_code < 600:
        return "provider_bad_gateway"
    return "provider_request_invalid"


def _int_value(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _elapsed_ms(started: float) -> int:
    return max(0, int((perf_counter() - started) * 1000))


def _slugify(value: str) -> str:
    chars = [
        char.lower() if char.isalnum() else "-" for char in value if char.isascii()
    ]
    slug = "-".join(part for part in "".join(chars).split("-") if part)
    return slug[:60] or "query"
