"""Visual image search adapter interfaces and implementations."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Protocol

from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.visual_image_search import (
    VisualImageSearchMatch,
    VisualImageSearchProviderError,
    VisualImageSearchRequest,
    VisualImageSearchResult,
)
from assistant_agent.services.provider_errors import (
    build_provider_error,
    map_exception_to_provider_error,
    sanitize_error_message,
)


DEFAULT_QWEN_IMAGE_SEARCH_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_QWEN_IMAGE_SEARCH_MODEL = "qwen3.7-plus"


class VisualImageSearchAdapter(Protocol):
    """Adapter contract for visual image search providers."""

    def search(self, request: VisualImageSearchRequest) -> VisualImageSearchResult:
        """Return structured visual image search results."""


class MockVisualImageSearchAdapter:
    """Deterministic local adapter for offline tests and demos."""

    provider = "mock"

    def search(self, request: VisualImageSearchRequest) -> VisualImageSearchResult:
        image_used = _first_http_image_ref(request)
        if not image_used:
            return _failed_visual_image_search_result(
                provider=self.provider,
                image_used="unsupported_image_ref",
                query_hint_used=_query_hint(request),
                code="provider_unsupported_input",
                message="visual_image_search v1 only supports public http or https image URLs.",
                recoverable=True,
            )

        slug = _slugify_image_ref(image_used)
        available = [
            VisualImageSearchMatch(
                title=f"Mock similar image 1 for {slug}",
                page_url=f"https://mock.example/visual-image-search/{slug}/page-1",
                image_url=f"https://mock.example/visual-image-search/{slug}/image-1.jpg",
                thumbnail_url=f"https://mock.example/visual-image-search/{slug}/thumb-1.jpg",
                source="mock-image-index",
                snippet="Stable offline visually similar image result.",
                similarity_score=0.91,
            ),
            VisualImageSearchMatch(
                title=f"Mock similar image 2 for {slug}",
                page_url=f"https://mock.example/visual-image-search/{slug}/page-2",
                image_url=f"https://mock.example/visual-image-search/{slug}/image-2.jpg",
                thumbnail_url=f"https://mock.example/visual-image-search/{slug}/thumb-2.jpg",
                source="mock-image-index",
                snippet="Second deterministic visual match.",
                similarity_score=0.84,
            ),
            VisualImageSearchMatch(
                title=f"Mock similar image 3 for {slug}",
                page_url=f"https://mock.example/visual-image-search/{slug}/page-3",
                image_url=f"https://mock.example/visual-image-search/{slug}/image-3.jpg",
                thumbnail_url=f"https://mock.example/visual-image-search/{slug}/thumb-3.jpg",
                source="mock-image-index",
                snippet="Additional offline visual context.",
                similarity_score=0.78,
            ),
        ]
        matches = available[: request.limit]
        return VisualImageSearchResult(
            image_used=image_used,
            query_hint_used=_query_hint(request),
            matches=matches,
            provider=self.provider,
            total=len(matches),
            latency_ms=1,
            output_ref=f"mock://visual_image_search/{slug}",
        )


@dataclass(frozen=True)
class QwenImageSearchConfig:
    """Configuration for the optional Qwen Responses API image_search adapter."""

    api_key: str | None
    base_url: str = DEFAULT_QWEN_IMAGE_SEARCH_BASE_URL
    model: str = DEFAULT_QWEN_IMAGE_SEARCH_MODEL
    timeout_seconds: float = 30.0


class QwenImageSearchAdapter:
    """HTTP adapter for Qwen Responses API image_search."""

    provider = "qwen"

    def __init__(self, config: QwenImageSearchConfig) -> None:
        self.config = config

    def search(self, request: VisualImageSearchRequest) -> VisualImageSearchResult:
        image_used = _first_http_image_ref(request)
        query_hint_used = _query_hint(request)
        if not self.config.api_key:
            return _failed_visual_image_search_result(
                provider=self.provider,
                image_used=image_used or "unsupported_image_ref",
                query_hint_used=query_hint_used,
                code="provider_unconfigured",
                message="qwen image_search provider is missing QWEN_IMAGE_SEARCH_API_KEY.",
                recoverable=True,
            )
        if not image_used:
            return _failed_visual_image_search_result(
                provider=self.provider,
                image_used="unsupported_image_ref",
                query_hint_used=query_hint_used,
                code="provider_unsupported_input",
                message="visual_image_search v1 only supports public http or https image URLs.",
                recoverable=True,
            )

        started = perf_counter()
        try:
            http_request = urllib.request.Request(
                qwen_image_search_responses_url(self.config.base_url),
                data=json.dumps(
                    build_qwen_image_search_payload(
                        image_url=image_used,
                        model=self.config.model,
                        query_hint=query_hint_used,
                    ),
                    ensure_ascii=False,
                ).encode("utf-8"),
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.config.api_key}",
                },
                method="POST",
            )
            with urllib.request.urlopen(
                http_request,
                timeout=self.config.timeout_seconds,
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return _failed_from_http_error(
                exc,
                image_used=image_used,
                query_hint_used=query_hint_used,
                latency_ms=_elapsed_ms(started),
            )
        except TimeoutError as exc:
            return _failed_from_exception(
                exc,
                image_used=image_used,
                query_hint_used=query_hint_used,
                code="provider_timeout",
                latency_ms=_elapsed_ms(started),
            )
        except urllib.error.URLError as exc:
            return _failed_from_exception(
                exc,
                image_used=image_used,
                query_hint_used=query_hint_used,
                code="provider_network_error",
                latency_ms=_elapsed_ms(started),
            )
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return _failed_from_exception(
                exc,
                image_used=image_used,
                query_hint_used=query_hint_used,
                code="provider_bad_response",
                latency_ms=_elapsed_ms(started),
            )
        except Exception as exc:  # pragma: no cover - defensive provider boundary
            return _failed_from_exception(
                exc,
                image_used=image_used,
                query_hint_used=query_hint_used,
                code="provider_unknown_error",
                latency_ms=_elapsed_ms(started),
            )

        return _qwen_result_from_payload(
            payload,
            request=request,
            image_used=image_used,
            query_hint_used=query_hint_used,
            latency_ms=_elapsed_ms(started),
        )


def create_visual_image_search_adapter(
    config: ProviderConfig | None = None,
) -> VisualImageSearchAdapter:
    """Create a visual image search adapter without initializing real clients."""

    resolved = config or ProviderConfig.from_env()
    if resolved.visual_image_search_provider == "qwen":
        return QwenImageSearchAdapter(
            QwenImageSearchConfig(
                api_key=resolved.qwen_image_search_api_key,
                base_url=resolved.qwen_image_search_base_url,
                model=resolved.qwen_image_search_model,
                timeout_seconds=resolved.qwen_image_search_timeout_seconds,
            )
        )
    return MockVisualImageSearchAdapter()


def build_qwen_image_search_payload(
    *,
    image_url: str,
    model: str = DEFAULT_QWEN_IMAGE_SEARCH_MODEL,
    query_hint: str | None = None,
) -> dict[str, Any]:
    """Build the Qwen Responses API payload for the image_search tool."""

    content: list[dict[str, str]] = []
    if query_hint and query_hint.strip():
        content.append({"type": "input_text", "text": query_hint.strip()})
    content.append({"type": "input_image", "image_url": image_url})
    return {
        "model": model,
        "input": [{"role": "user", "content": content}],
        "tools": [{"type": "image_search"}],
    }


def qwen_image_search_responses_url(base_url: str) -> str:
    """Return the Qwen Responses API endpoint for a base URL or full endpoint."""

    normalized = (base_url or DEFAULT_QWEN_IMAGE_SEARCH_BASE_URL).rstrip("/")
    if normalized.endswith("/responses"):
        return normalized
    return f"{normalized}/responses"


def _qwen_result_from_payload(
    payload: Any,
    *,
    request: VisualImageSearchRequest,
    image_used: str,
    query_hint_used: str | None,
    latency_ms: int,
) -> VisualImageSearchResult:
    if not isinstance(payload, dict):
        return _failed_visual_image_search_result(
            provider="qwen",
            image_used=image_used,
            query_hint_used=query_hint_used,
            code="provider_bad_response",
            message="qwen image_search provider returned a non-object JSON response.",
            recoverable=False,
            latency_ms=latency_ms,
        )

    search_call = _image_search_call(payload.get("output"))
    if search_call is None:
        return _failed_visual_image_search_result(
            provider="qwen",
            image_used=image_used,
            query_hint_used=query_hint_used,
            code="provider_schema_mismatch",
            message="qwen image_search response must include image_search_call output.",
            recoverable=False,
            latency_ms=latency_ms,
        )

    raw_images = search_call.get("output")
    try:
        raw_items = json.loads(raw_images) if isinstance(raw_images, str) else raw_images
    except json.JSONDecodeError:
        return _failed_visual_image_search_result(
            provider="qwen",
            image_used=image_used,
            query_hint_used=query_hint_used,
            code="provider_bad_response",
            message="qwen image_search_call output was not valid JSON.",
            recoverable=False,
            latency_ms=latency_ms,
        )
    if not isinstance(raw_items, list):
        return _failed_visual_image_search_result(
            provider="qwen",
            image_used=image_used,
            query_hint_used=query_hint_used,
            code="provider_schema_mismatch",
            message="qwen image_search_call output must be a JSON array.",
            recoverable=False,
            latency_ms=latency_ms,
        )

    matches = [
        match
        for match in (_match_from_qwen_item(item) for item in raw_items)
        if match is not None
    ][: request.limit]
    if not matches:
        return _failed_visual_image_search_result(
            provider="qwen",
            image_used=image_used,
            query_hint_used=query_hint_used,
            code="provider_empty_response",
            message="qwen image_search returned no usable image matches.",
            recoverable=True,
            latency_ms=latency_ms,
        )
    response_id = _response_id(payload)
    return VisualImageSearchResult(
        image_used=image_used,
        query_hint_used=query_hint_used,
        matches=matches,
        provider="qwen",
        total=len(matches),
        latency_ms=latency_ms,
        output_ref=f"qwen://image_search/{response_id}" if response_id else None,
    )


def _image_search_call(output: Any) -> dict[str, Any] | None:
    if not isinstance(output, list):
        return None
    for item in output:
        if isinstance(item, dict) and item.get("type") == "image_search_call":
            return item
    return None


def _match_from_qwen_item(item: Any) -> VisualImageSearchMatch | None:
    if not isinstance(item, dict):
        return None
    page_url = _http_url_value(
        item.get("page_url")
        or item.get("pageUrl")
        or item.get("source_url")
        or item.get("sourceUrl")
        or item.get("url")
    )
    image_url = _http_url_value(
        item.get("image_url")
        or item.get("imageUrl")
        or item.get("content_url")
        or item.get("contentUrl")
        or item.get("url")
    )
    thumbnail_url = _http_url_value(
        item.get("thumbnail_url") or item.get("thumbnailUrl") or item.get("thumbnail")
    )
    if not page_url and not image_url:
        return None
    if not image_url:
        image_url = thumbnail_url or page_url
    if not page_url:
        page_url = image_url
    return VisualImageSearchMatch(
        title=sanitize_error_message(item.get("title") or item.get("name") or "Image search result"),
        page_url=page_url,
        image_url=image_url,
        thumbnail_url=thumbnail_url,
        source=_source_value(item, page_url=page_url),
        snippet=sanitize_error_message(
            item.get("snippet") or item.get("description") or item.get("summary") or ""
        ),
        similarity_score=_score_value(
            item.get("similarity_score") or item.get("similarityScore") or item.get("score")
        ),
    )


def _source_value(item: dict[str, Any], *, page_url: str) -> str | None:
    source = item.get("source") or item.get("site_name") or item.get("siteName")
    if isinstance(source, str) and source.strip():
        return sanitize_error_message(source)
    hostname = urllib.parse.urlparse(page_url).hostname
    return hostname or None


def _score_value(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if score < 0:
        return None
    if score > 1 and score <= 100:
        score = score / 100
    if score > 1:
        return None
    return round(score, 4)


def _first_http_image_ref(request: VisualImageSearchRequest) -> str | None:
    if isinstance(request.image_url, str) and _is_http_url(request.image_url):
        return request.image_url.strip()
    for image_id in request.image_ids:
        if isinstance(image_id, str) and _is_http_url(image_id):
            return image_id.strip()
    return None


def _query_hint(request: VisualImageSearchRequest) -> str | None:
    if isinstance(request.query_hint, str) and request.query_hint.strip():
        return request.query_hint.strip()
    return None


def _http_url_value(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    stripped = value.strip()
    return stripped if _is_http_url(stripped) else ""


def _is_http_url(value: str) -> bool:
    lowered = value.strip().lower()
    return lowered.startswith("http://") or lowered.startswith("https://")


def _response_id(payload: dict[str, Any]) -> str | None:
    value = payload.get("id") or payload.get("response_id") or payload.get("request_id")
    return value if isinstance(value, str) and value.strip() else None


def _failed_from_http_error(
    exc: urllib.error.HTTPError,
    *,
    image_used: str,
    query_hint_used: str | None,
    latency_ms: int,
) -> VisualImageSearchResult:
    body = _read_http_error_body(exc)
    message = body.get("message") if isinstance(body.get("message"), str) else f"HTTP {exc.code}"
    return _failed_visual_image_search_result(
        provider="qwen",
        image_used=image_used,
        query_hint_used=query_hint_used,
        code=_http_error_code(exc.code),
        message=f"qwen image_search provider returned {message}.",
        recoverable=exc.code in {408, 429, 500, 502, 503, 504},
        latency_ms=latency_ms,
    )


def _failed_from_exception(
    exc: BaseException,
    *,
    image_used: str,
    query_hint_used: str | None,
    code: str,
    latency_ms: int,
) -> VisualImageSearchResult:
    error = map_exception_to_provider_error(
        exc,
        provider="qwen",
        capability="visual_image_search",
        code=code,
    )
    return _failed_visual_image_search_result(
        provider="qwen",
        image_used=image_used,
        query_hint_used=query_hint_used,
        code=error.code,
        message=error.message,
        recoverable=error.recoverable,
        latency_ms=latency_ms,
    )


def _failed_visual_image_search_result(
    *,
    provider: str,
    image_used: str,
    query_hint_used: str | None,
    code: str,
    message: str,
    recoverable: bool | None = None,
    latency_ms: int | None = None,
) -> VisualImageSearchResult:
    error = build_provider_error(
        code,
        message,
        recoverable=recoverable,
        provider=provider,
        capability="visual_image_search",
    )
    return VisualImageSearchResult(
        image_used=image_used if _is_http_url(image_used) else "unsupported_image_ref",
        query_hint_used=query_hint_used,
        matches=[],
        provider=provider,
        total=0,
        errors=[
            VisualImageSearchProviderError(
                code=error.code,
                message=error.message,
                recoverable=error.recoverable,
            )
        ],
        latency_ms=latency_ms,
    )


def _read_http_error_body(exc: urllib.error.HTTPError) -> dict[str, Any]:
    try:
        parsed = json.loads(exc.read().decode("utf-8"))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _http_error_code(status: int) -> str:
    if status == 401:
        return "provider_auth_failed"
    if status == 403:
        return "provider_permission_denied"
    if status == 408:
        return "provider_timeout"
    if status == 429:
        return "provider_rate_limited"
    if status >= 500:
        return "provider_bad_gateway"
    if status >= 400:
        return "provider_bad_response"
    return "provider_execution_failed"


def _elapsed_ms(started: float) -> int:
    return max(0, int((perf_counter() - started) * 1000))


def _slugify_image_ref(value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    candidate = f"{parsed.netloc}{parsed.path}" if parsed.netloc else value
    slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in candidate)
    slug = "-".join(part for part in slug.split("-") if part)
    return slug[:80] or "image"
