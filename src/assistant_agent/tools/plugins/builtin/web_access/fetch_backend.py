"""Plugin-private web fetch adapter interfaces and implementations."""

from __future__ import annotations

import json
import ipaddress
import urllib.error
import urllib.request
from time import perf_counter
from typing import Any, Protocol
from urllib.parse import urlparse, urlunparse

from assistant_agent.config import ProviderConfig
from assistant_agent.tools.plugins.builtin.web_access.fetch_models import (
    WebFetchProviderError,
    WebFetchRequest,
    WebFetchResult,
)
from assistant_agent.providers.provider_errors import (
    build_provider_error,
    map_exception_to_provider_error,
)
from assistant_agent.tools.ids import WEB_FETCH_CAPABILITY


class WebFetchAdapter(Protocol):
    """Adapter contract for web page readable-content fetch providers."""

    def fetch(self, request: WebFetchRequest) -> WebFetchResult:
        """Return structured readable content for one URL."""


class MockWebFetchAdapter:
    """Deterministic local adapter for offline tests and demos."""

    provider = "mock"

    def fetch(self, request: WebFetchRequest) -> WebFetchResult:
        url = request.url.strip()
        if not url:
            return _failed_web_fetch_result(
                provider=self.provider,
                url=request.url or WEB_FETCH_CAPABILITY,
                code="provider_request_invalid",
                message="web_fetch requires url.",
                recoverable=False,
                content_format=request.content_format,
            )
        if not _is_http_url(url):
            return _failed_web_fetch_result(
                provider=self.provider,
                url=url,
                code="provider_request_invalid",
                message="web_fetch requires http or https url.",
                recoverable=False,
                content_format=request.content_format,
            )

        content = (
            f"# Mock page for {url}\n\n"
            f"Stable offline fetched content for {url}. "
            "This mock body is deterministic and safe for local tests."
        )
        bounded_content, truncated = _bound_content(content, request.max_chars)
        return WebFetchResult(
            url=url,
            title=f"Mock page for {url}",
            content=bounded_content,
            content_format=request.content_format,
            provider=self.provider,
            total_chars=len(content),
            truncated=truncated,
            latency_ms=1,
            output_ref=f"mock://web_fetch/{_slugify_url(url)}",
        )


class HttpWebFetchAdapter:
    """Generic HTTP web fetch adapter.

    The backend is expected to accept a JSON POST body with url, max_chars, and
    content_format, and to return JSON containing readable content or errors.
    If the configured web search base URL ends in /search, this adapter uses the
    sibling /fetch endpoint for the same relay.
    """

    provider = "http"

    def __init__(
        self,
        base_url: str | None,
        api_key: str | None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.base_url = _fetch_url_from_base_url(base_url) if base_url else None
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self._opener = _loopback_proxy_bypass_opener(self.base_url)

    def fetch(self, request: WebFetchRequest) -> WebFetchResult:
        missing = []
        if not self.base_url:
            missing.append("WEB_SEARCH_BASE_URL")
        if not self.api_key:
            missing.append("WEB_SEARCH_API_KEY")
        if missing:
            return _failed_web_fetch_result(
                provider=self.provider,
                url=request.url,
                code="provider_unconfigured",
                message=f"http web fetch provider is missing {', '.join(missing)}.",
                recoverable=True,
                content_format=request.content_format,
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
            return _failed_web_fetch_result(
                provider=self.provider,
                url=request.url,
                code=_http_error_code(exc.code),
                message=f"http web fetch provider returned HTTP {exc.code}.",
                recoverable=exc.code in {408, 429, 500, 502, 503, 504},
                content_format=request.content_format,
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

        return _web_fetch_result_from_payload(
            payload, request=request, latency_ms=_elapsed_ms(started)
        )


class TavilyWebFetchAdapter:
    """In-process Tavily Extract API adapter."""

    provider = "tavily"

    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.base_url = _provider_endpoint(base_url, "extract")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def fetch(self, request: WebFetchRequest) -> WebFetchResult:
        if not self.api_key:
            return _failed_web_fetch_result(
                provider=self.provider,
                url=request.url,
                code="provider_unconfigured",
                message="tavily web fetch provider is missing TAVILY_API_KEY.",
                recoverable=True,
                content_format=request.content_format,
            )

        started = perf_counter()
        try:
            http_request = urllib.request.Request(
                self.base_url,
                data=json.dumps(
                    {
                        "urls": request.url,
                        "extract_depth": "basic",
                        "format": request.content_format,
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
                method="POST",
            )
            with urllib.request.urlopen(
                http_request,
                timeout=self.timeout_seconds,
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return _failed_web_fetch_result(
                provider=self.provider,
                url=request.url,
                code=_http_error_code(exc.code),
                message=f"tavily web fetch provider returned HTTP {exc.code}.",
                recoverable=exc.code in {408, 429, 500, 502, 503, 504},
                content_format=request.content_format,
                latency_ms=_elapsed_ms(started),
            )
        except urllib.error.URLError as exc:
            return _failed_from_exception(
                exc,
                request=request,
                code="provider_network_error",
                latency_ms=_elapsed_ms(started),
                provider=self.provider,
            )
        except TimeoutError as exc:
            return _failed_from_exception(
                exc,
                request=request,
                code="provider_timeout",
                latency_ms=_elapsed_ms(started),
                provider=self.provider,
            )
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return _failed_from_exception(
                exc,
                request=request,
                code="provider_bad_response",
                latency_ms=_elapsed_ms(started),
                provider=self.provider,
            )
        except Exception as exc:  # pragma: no cover - defensive provider boundary
            return _failed_from_exception(
                exc,
                request=request,
                code="provider_unknown_error",
                latency_ms=_elapsed_ms(started),
                provider=self.provider,
            )

        return _web_fetch_result_from_payload(
            _normalize_tavily_extract_payload(payload, request=request),
            request=request,
            latency_ms=_elapsed_ms(started),
        )


def create_web_fetch_adapter(config: ProviderConfig | None = None) -> WebFetchAdapter:
    """Create a web fetch adapter without initializing real provider clients."""

    resolved = config or ProviderConfig.from_env()
    if resolved.search_provider == "http":
        return HttpWebFetchAdapter(
            base_url=resolved.web_search_base_url,
            api_key=resolved.web_search_api_key,
            timeout_seconds=resolved.web_search_timeout_seconds,
        )
    if resolved.search_provider == "tavily":
        return TavilyWebFetchAdapter(
            base_url=resolved.tavily_base_url,
            api_key=resolved.tavily_api_key,
            timeout_seconds=resolved.web_search_timeout_seconds,
        )
    if resolved.provider_mode == "real":
        raise ValueError("real provider mode requires a configured web fetch provider")
    return MockWebFetchAdapter()


def _request_payload(request: WebFetchRequest) -> dict[str, Any]:
    payload = request.model_dump(mode="json")
    return {
        key: value for key, value in payload.items() if value not in (None, "", [], {})
    }


def _normalize_tavily_extract_payload(
    payload: Any,
    *,
    request: WebFetchRequest,
) -> Any:
    if not isinstance(payload, dict):
        return payload
    raw_results = payload.get("results")
    if not isinstance(raw_results, list) or not raw_results:
        return {
            "provider": "tavily",
            "url": request.url,
            "content_format": request.content_format,
            "errors": [
                {
                    "code": "provider_empty_response",
                    "message": "tavily extract returned no usable results.",
                    "recoverable": True,
                }
            ],
        }
    first = raw_results[0]
    if not isinstance(first, dict):
        return payload
    content = first.get("raw_content")
    if not isinstance(content, str):
        content = first.get("content")
    normalized: dict[str, Any] = {
        "provider": "tavily",
        "url": first.get("url") or request.url,
        "content": content,
        "content_format": request.content_format,
    }
    if isinstance(payload.get("request_id"), str):
        normalized["output_ref"] = f"tavily://extract/{payload['request_id']}"
    return normalized


def _provider_endpoint(base_url: str, endpoint: str) -> str:
    stripped = base_url.rstrip("/")
    suffix = f"/{endpoint}"
    return stripped if stripped.endswith(suffix) else f"{stripped}{suffix}"


def _web_fetch_result_from_payload(
    payload: Any, *, request: WebFetchRequest, latency_ms: int
) -> WebFetchResult:
    if not isinstance(payload, dict):
        return _failed_web_fetch_result(
            provider="http",
            url=request.url,
            code="provider_bad_response",
            message="http web fetch provider returned a non-object JSON response.",
            recoverable=False,
            content_format=request.content_format,
            latency_ms=latency_ms,
        )

    provider = str(payload.get("provider") or "http")
    url = str(payload.get("url") or request.url)
    content_format = _content_format_from_payload(payload, request=request)
    backend_errors = _errors_from_payload(payload.get("errors"), provider=provider)
    content = _string_payload_value(payload.get("content"))
    if content is None:
        content = _string_payload_value(payload.get("raw_content"))
    if content is None:
        if backend_errors:
            content = ""
        else:
            return _failed_web_fetch_result(
                provider=provider,
                url=url,
                code="provider_schema_mismatch",
                message="http web fetch provider response must include content.",
                recoverable=False,
                content_format=content_format,
                latency_ms=latency_ms,
            )

    bounded_content, was_bounded = _bound_content(content, request.max_chars)
    total_chars = _int_value(payload.get("total_chars"), default=len(content))
    truncated = (
        bool(payload.get("truncated"))
        or was_bounded
        or total_chars > len(bounded_content)
    )
    title = payload.get("title") if isinstance(payload.get("title"), str) else None
    output_ref = (
        payload.get("output_ref") if isinstance(payload.get("output_ref"), str) else None
    )
    if backend_errors:
        return WebFetchResult(
            url=url,
            title=title,
            content=bounded_content,
            content_format=content_format,
            provider=provider,
            total_chars=total_chars,
            truncated=truncated,
            latency_ms=latency_ms,
            output_ref=output_ref,
            errors=backend_errors,
        )
    if not bounded_content.strip():
        return _failed_web_fetch_result(
            provider=provider,
            url=url,
            code="provider_empty_response",
            message="http web fetch provider returned no usable content.",
            recoverable=True,
            content_format=content_format,
            latency_ms=latency_ms,
        )
    return WebFetchResult(
        url=url,
        title=title,
        content=bounded_content,
        content_format=content_format,
        provider=provider,
        total_chars=total_chars,
        truncated=truncated,
        latency_ms=latency_ms,
        output_ref=output_ref,
    )


def _errors_from_payload(payload: Any, *, provider: str) -> list[WebFetchProviderError]:
    if not isinstance(payload, list):
        return []
    errors: list[WebFetchProviderError] = []
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
            capability=WEB_FETCH_CAPABILITY,
        )
        errors.append(
            WebFetchProviderError(
                code=error.code,
                message=error.message,
                recoverable=error.recoverable,
            )
        )
    return errors


def _content_format_from_payload(
    payload: dict[str, Any], *, request: WebFetchRequest
) -> str:
    value = payload.get("content_format") or payload.get("format")
    if value in {"markdown", "text"}:
        return value
    return request.content_format


def _failed_from_exception(
    exc: BaseException,
    *,
    request: WebFetchRequest,
    code: str,
    latency_ms: int,
    provider: str = "http",
) -> WebFetchResult:
    error = map_exception_to_provider_error(
        exc, provider=provider, capability=WEB_FETCH_CAPABILITY, code=code
    )
    return WebFetchResult(
        url=request.url,
        provider=provider,
        content_format=request.content_format,
        errors=[
            WebFetchProviderError(
                code=error.code, message=error.message, recoverable=error.recoverable
            )
        ],
        latency_ms=latency_ms,
        total_chars=0,
    )


def _failed_web_fetch_result(
    *,
    provider: str,
    url: str,
    code: str,
    message: str,
    recoverable: bool,
    content_format: str,
    latency_ms: int | None = None,
) -> WebFetchResult:
    error = build_provider_error(
        code,
        message,
        recoverable=recoverable,
        provider=provider,
        capability=WEB_FETCH_CAPABILITY,
    )
    return WebFetchResult(
        url=url or WEB_FETCH_CAPABILITY,
        provider=provider,
        content_format=content_format,  # type: ignore[arg-type]
        errors=[
            WebFetchProviderError(
                code=error.code, message=error.message, recoverable=error.recoverable
            )
        ],
        latency_ms=latency_ms,
        total_chars=0,
    )


def _fetch_url_from_base_url(base_url: str) -> str:
    stripped = base_url.strip()
    parsed = urlparse(stripped)
    path = parsed.path or ""
    parts = [part for part in path.split("/") if part]
    if parts and parts[-1] == "fetch":
        fetch_path = "/" + "/".join(parts)
    elif parts and parts[-1] == "search":
        parts[-1] = "fetch"
        fetch_path = "/" + "/".join(parts)
    else:
        fetch_path = f"{path.rstrip('/')}/fetch" if path else "/fetch"
    return urlunparse(parsed._replace(path=fetch_path, query="", fragment=""))


def _loopback_proxy_bypass_opener(base_url: str | None) -> Any | None:
    if not _should_bypass_proxy_for_base_url(base_url):
        return None
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _should_bypass_proxy_for_base_url(base_url: str | None) -> bool:
    host = urlparse(base_url or "").hostname
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _string_payload_value(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _bound_content(content: str, max_chars: int) -> tuple[str, bool]:
    if len(content) <= max_chars:
        return content, False
    return content[:max_chars], True


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


def _slugify_url(value: str) -> str:
    parsed = urlparse(value)
    text = f"{parsed.netloc}{parsed.path}".strip("/") or value
    chars = [
        char.lower() if char.isalnum() else "-" for char in text if char.isascii()
    ]
    slug = "-".join(part for part in "".join(chars).split("-") if part)
    return slug[:80] or "url"
