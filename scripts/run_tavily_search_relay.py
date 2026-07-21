#!/usr/bin/env python3
"""Run a local Tavily-backed HTTP relay for web search and fetch tools."""

# ruff: noqa: E402 - repository src path must be installed before package imports.

from __future__ import annotations

import argparse
import hmac
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from assistant_agent.services.provider_errors import build_provider_error


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7005
DEFAULT_PATH = "/search"
DEFAULT_TAVILY_BASE_URL = "https://api.tavily.com/search"
DEFAULT_TAVILY_EXTRACT_BASE_URL = "https://api.tavily.com/extract"
DEFAULT_SEARCH_DEPTH = "basic"
DEFAULT_EXTRACT_DEPTH = "basic"
DEFAULT_EXTRACT_FORMAT = "markdown"
DEFAULT_TIMEOUT_SECONDS = 10.0
ALLOWED_SEARCH_DEPTHS = frozenset({"basic", "advanced", "fast", "ultra-fast"})
ALLOWED_EXTRACT_DEPTHS = frozenset({"basic", "advanced"})
ALLOWED_EXTRACT_FORMATS = frozenset({"markdown", "text"})

UrlOpener = Callable[..., Any]


@dataclass(frozen=True)
class RelayConfig:
    relay_api_key: str
    tavily_api_key: str
    tavily_base_url: str = DEFAULT_TAVILY_BASE_URL
    tavily_extract_base_url: str = DEFAULT_TAVILY_EXTRACT_BASE_URL
    search_depth: str = DEFAULT_SEARCH_DEPTH
    extract_depth: str = DEFAULT_EXTRACT_DEPTH
    extract_format: str = DEFAULT_EXTRACT_FORMAT
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "RelayConfig":
        source = environ or os.environ
        missing = [
            key
            for key in ("WEB_SEARCH_RELAY_API_KEY", "TAVILY_API_KEY")
            if not source.get(key)
        ]
        if missing:
            raise ValueError(
                "tavily search relay is missing required env: "
                + ", ".join(missing)
            )

        search_depth = (
            source.get("TAVILY_SEARCH_DEPTH") or DEFAULT_SEARCH_DEPTH
        ).strip()
        if search_depth not in ALLOWED_SEARCH_DEPTHS:
            raise ValueError(
                "TAVILY_SEARCH_DEPTH must be one of: "
                + ", ".join(sorted(ALLOWED_SEARCH_DEPTHS))
            )
        extract_depth = (
            source.get("TAVILY_EXTRACT_DEPTH") or DEFAULT_EXTRACT_DEPTH
        ).strip()
        if extract_depth not in ALLOWED_EXTRACT_DEPTHS:
            raise ValueError(
                "TAVILY_EXTRACT_DEPTH must be one of: "
                + ", ".join(sorted(ALLOWED_EXTRACT_DEPTHS))
            )
        extract_format = (
            source.get("TAVILY_EXTRACT_FORMAT") or DEFAULT_EXTRACT_FORMAT
        ).strip()
        if extract_format not in ALLOWED_EXTRACT_FORMATS:
            raise ValueError(
                "TAVILY_EXTRACT_FORMAT must be one of: "
                + ", ".join(sorted(ALLOWED_EXTRACT_FORMATS))
            )

        return cls(
            relay_api_key=source["WEB_SEARCH_RELAY_API_KEY"],
            tavily_api_key=source["TAVILY_API_KEY"],
            tavily_base_url=(
                source.get("TAVILY_SEARCH_BASE_URL") or DEFAULT_TAVILY_BASE_URL
            ).strip(),
            tavily_extract_base_url=(
                source.get("TAVILY_EXTRACT_BASE_URL")
                or DEFAULT_TAVILY_EXTRACT_BASE_URL
            ).strip(),
            search_depth=search_depth,
            extract_depth=extract_depth,
            extract_format=extract_format,
            timeout_seconds=_float_env(
                source.get("TAVILY_TIMEOUT_SECONDS"), DEFAULT_TIMEOUT_SECONDS
            ),
        )


class RelayRequestError(ValueError):
    def __init__(self, code: str, message: str, query_used: str = "web_search") -> None:
        self.code = code
        self.message = message
        self.query_used = query_used
        super().__init__(message)


def handle_relay_request(
    body: bytes,
    headers: Mapping[str, str],
    config: RelayConfig,
    *,
    opener: UrlOpener | None = None,
) -> tuple[int, dict[str, Any]]:
    auth_error = _auth_error(headers, config)
    if auth_error is not None:
        status, code, message = auth_error
        return status, _search_error_payload(
            query_used="web_search",
            code=code,
            message=message,
            recoverable=False,
        )

    try:
        relay_payload = _json_object_from_body(body)
        tavily_payload = build_tavily_payload(relay_payload, config)
    except RelayRequestError as exc:
        return HTTPStatus.BAD_REQUEST, _search_error_payload(
            query_used=exc.query_used,
            code=exc.code,
            message=exc.message,
            recoverable=False,
        )

    return _call_tavily(tavily_payload, config, opener=opener)


def handle_fetch_request(
    body: bytes,
    headers: Mapping[str, str],
    config: RelayConfig,
    *,
    opener: UrlOpener | None = None,
) -> tuple[int, dict[str, Any]]:
    auth_error = _auth_error(headers, config)
    if auth_error is not None:
        status, code, message = auth_error
        return status, _fetch_error_payload(
            url="web_fetch",
            code=code,
            message=message,
            recoverable=False,
            content_format=config.extract_format,
        )

    try:
        relay_payload = _json_object_from_body(body)
        tavily_payload = build_tavily_extract_payload(relay_payload, config)
        url = _extract_url_from_payload(relay_payload)
        max_chars = _bounded_int(
            relay_payload.get("max_chars"), default=6000, low=1, high=20000
        )
        content_format = _extract_format_from_payload(relay_payload, config)
    except RelayRequestError as exc:
        return HTTPStatus.BAD_REQUEST, _fetch_error_payload(
            url=exc.query_used,
            code=exc.code,
            message=exc.message,
            recoverable=False,
            content_format=config.extract_format,
        )

    return _call_tavily_extract(
        tavily_payload,
        config,
        url=url,
        max_chars=max_chars,
        content_format=content_format,
        opener=opener,
    )


def build_tavily_payload(
    relay_payload: Mapping[str, Any], config: RelayConfig
) -> dict[str, Any]:
    query = _string_value(relay_payload.get("query")).strip()
    if not query:
        raise RelayRequestError(
            "provider_request_invalid", "web_search relay requires non-empty query."
        )

    payload: dict[str, Any] = {
        "query": query,
        "max_results": _bounded_int(
            relay_payload.get("limit"), default=5, low=1, high=10
        ),
        "search_depth": config.search_depth,
        "include_answer": True,
        "include_raw_content": False,
        "include_images": False,
    }
    domain = _domain_from_site_filter(relay_payload.get("site_filter"))
    if domain:
        payload["include_domains"] = [domain]
    time_range = _time_range_from_recency(relay_payload.get("recency_days"))
    if time_range:
        payload["time_range"] = time_range
    return payload


def build_tavily_extract_payload(
    relay_payload: Mapping[str, Any], config: RelayConfig
) -> dict[str, Any]:
    url = _extract_url_from_payload(relay_payload)
    content_format = _extract_format_from_payload(relay_payload, config)
    return {
        "urls": [url],
        "extract_depth": config.extract_depth,
        "include_images": False,
        "format": content_format,
        "timeout": _bounded_int(
            int(config.timeout_seconds), default=10, low=1, high=60
        ),
    }


def make_handler(
    config: RelayConfig,
    *,
    path: str = DEFAULT_PATH,
    opener: UrlOpener | None = None,
) -> type[BaseHTTPRequestHandler]:
    configured_path = _normalize_path(path)
    configured_fetch_path = _fetch_path_for_search_path(configured_path)
    configured_opener = opener or urllib.request.urlopen

    class TavilySearchRelayHandler(BaseHTTPRequestHandler):
        server_version = "TavilyRelay/1.0"

        def do_POST(self) -> None:
            request_path = urlparse(self.path).path
            if request_path not in {configured_path, configured_fetch_path}:
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    _search_error_payload(
                        query_used="web_search",
                        code="provider_request_invalid",
                        message=f"unsupported relay path: {request_path}",
                        recoverable=False,
                    ),
                )
                return

            try:
                content_length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                content_length = 0
            body = self.rfile.read(max(0, content_length))
            if request_path == configured_fetch_path:
                status, payload = handle_fetch_request(
                    body, self.headers, config, opener=configured_opener
                )
            else:
                status, payload = handle_relay_request(
                    body, self.headers, config, opener=configured_opener
                )
            self._send_json(status, payload)

        def do_GET(self) -> None:
            self._send_json(
                HTTPStatus.METHOD_NOT_ALLOWED,
                _search_error_payload(
                    query_used="web_search",
                    code="provider_request_invalid",
                    message="tavily relay only accepts POST.",
                    recoverable=False,
                ),
            )

        def _send_json(self, status: int, payload: Mapping[str, Any]) -> None:
            response = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(int(status))
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

    return TavilySearchRelayHandler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a local Tavily-backed relay for WEB_SEARCH_BASE_URL."
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="Host interface to bind.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port to bind.")
    parser.add_argument(
        "--path",
        default=DEFAULT_PATH,
        help="HTTP path that accepts web_search POST requests.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the relay startup message.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = RelayConfig.from_env()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    server = ThreadingHTTPServer(
        (args.host, args.port), make_handler(config, path=args.path)
    )
    relay_url = f"http://{args.host}:{args.port}{_normalize_path(args.path)}"
    fetch_url = (
        f"http://{args.host}:{args.port}"
        f"{_fetch_path_for_search_path(_normalize_path(args.path))}"
    )
    if not args.quiet:
        print(
            f"tavily relay listening on {relay_url} (fetch at {fetch_url})",
            flush=True,
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        server.server_close()
    return 0


def _call_tavily(
    tavily_payload: Mapping[str, Any],
    config: RelayConfig,
    *,
    opener: UrlOpener | None = None,
) -> tuple[int, dict[str, Any]]:
    query_used = str(tavily_payload.get("query") or "web_search")
    request = urllib.request.Request(
        config.tavily_base_url,
        data=json.dumps(tavily_payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.tavily_api_key}",
        },
        method="POST",
    )

    try:
        with (opener or urllib.request.urlopen)(
            request, timeout=config.timeout_seconds
        ) as response:
            raw_payload = response.read()
    except urllib.error.HTTPError as exc:
        code = _http_error_code(exc.code)
        message = _http_error_message(exc, operation="search")
        return HTTPStatus.OK, _search_error_payload(
            query_used=query_used,
            code=code,
            message=message,
            recoverable=None,
        )
    except TimeoutError as exc:
        return HTTPStatus.OK, _search_error_payload(
            query_used=query_used,
            code="provider_timeout",
            message=str(exc),
            recoverable=True,
        )
    except urllib.error.URLError as exc:
        return HTTPStatus.OK, _search_error_payload(
            query_used=query_used,
            code="provider_network_error",
            message=str(exc.reason or exc),
            recoverable=True,
        )
    except Exception as exc:  # pragma: no cover - defensive provider boundary
        return HTTPStatus.OK, _search_error_payload(
            query_used=query_used,
            code="provider_unknown_error",
            message=str(exc),
            recoverable=None,
        )

    try:
        payload = json.loads(raw_payload.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return HTTPStatus.OK, _search_error_payload(
            query_used=query_used,
            code="provider_bad_response",
            message=str(exc),
            recoverable=False,
        )

    return HTTPStatus.OK, _normalize_tavily_response(payload, query_used=query_used)


def _call_tavily_extract(
    tavily_payload: Mapping[str, Any],
    config: RelayConfig,
    *,
    url: str,
    max_chars: int,
    content_format: str,
    opener: UrlOpener | None = None,
) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        config.tavily_extract_base_url,
        data=json.dumps(tavily_payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.tavily_api_key}",
        },
        method="POST",
    )

    try:
        with (opener or urllib.request.urlopen)(
            request, timeout=config.timeout_seconds
        ) as response:
            raw_payload = response.read()
    except urllib.error.HTTPError as exc:
        code = _http_error_code(exc.code)
        message = _http_error_message(exc, operation="extract")
        return HTTPStatus.OK, _fetch_error_payload(
            url=url,
            code=code,
            message=message,
            recoverable=None,
            content_format=content_format,
        )
    except TimeoutError as exc:
        return HTTPStatus.OK, _fetch_error_payload(
            url=url,
            code="provider_timeout",
            message=str(exc),
            recoverable=True,
            content_format=content_format,
        )
    except urllib.error.URLError as exc:
        return HTTPStatus.OK, _fetch_error_payload(
            url=url,
            code="provider_network_error",
            message=str(exc.reason or exc),
            recoverable=True,
            content_format=content_format,
        )
    except Exception as exc:  # pragma: no cover - defensive provider boundary
        return HTTPStatus.OK, _fetch_error_payload(
            url=url,
            code="provider_unknown_error",
            message=str(exc),
            recoverable=None,
            content_format=content_format,
        )

    try:
        payload = json.loads(raw_payload.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return HTTPStatus.OK, _fetch_error_payload(
            url=url,
            code="provider_bad_response",
            message=str(exc),
            recoverable=False,
            content_format=content_format,
        )

    return HTTPStatus.OK, _normalize_tavily_extract_response(
        payload,
        requested_url=url,
        max_chars=max_chars,
        content_format=content_format,
    )


def _normalize_tavily_response(payload: Any, *, query_used: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return _search_error_payload(
            query_used=query_used,
            code="provider_bad_response",
            message="Tavily search returned a non-object JSON response.",
            recoverable=False,
        )

    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        return _search_error_payload(
            query_used=query_used,
            code="provider_schema_mismatch",
            message="Tavily search response must include a results list.",
            recoverable=False,
        )

    resolved_query = _string_value(payload.get("query")).strip() or query_used
    results = [_result_item_from_payload(item) for item in raw_results]
    normalized_results = [item for item in results if item is not None]
    if not normalized_results:
        return _search_error_payload(
            query_used=resolved_query,
            code="provider_empty_response",
            message="Tavily search returned no usable results.",
            recoverable=True,
        )

    output: dict[str, Any] = {
        "provider": "tavily",
        "query_used": resolved_query,
        "results": normalized_results,
        "total": len(normalized_results),
        "output_ref": _output_ref(payload, resolved_query),
    }
    summary = _string_value(payload.get("answer") or payload.get("summary")).strip()
    if summary:
        output["summary"] = summary
    return output


def _normalize_tavily_extract_response(
    payload: Any,
    *,
    requested_url: str,
    max_chars: int,
    content_format: str,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return _fetch_error_payload(
            url=requested_url,
            code="provider_bad_response",
            message="Tavily extract returned a non-object JSON response.",
            recoverable=False,
            content_format=content_format,
        )

    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        return _fetch_error_payload(
            url=requested_url,
            code="provider_schema_mismatch",
            message="Tavily extract response must include a results list.",
            recoverable=False,
            content_format=content_format,
        )

    result = _first_extract_result(raw_results)
    if result is None:
        failed_message = _failed_extract_message(payload.get("failed_results"))
        return _fetch_error_payload(
            url=requested_url,
            code="provider_execution_failed" if failed_message else "provider_empty_response",
            message=failed_message or "Tavily extract returned no usable content.",
            recoverable=True,
            content_format=content_format,
        )

    resolved_url = _string_value(result.get("url")).strip() or requested_url
    content = _string_value(
        result.get("raw_content") or result.get("content") or result.get("text")
    )
    total_chars = len(content)
    bounded_content = content[:max_chars]
    title = _string_value(result.get("title")).strip()
    output: dict[str, Any] = {
        "provider": "tavily",
        "url": resolved_url,
        "content": bounded_content,
        "content_format": content_format,
        "total_chars": total_chars,
        "truncated": total_chars > len(bounded_content),
        "output_ref": _extract_output_ref(payload, resolved_url),
    }
    if title:
        output["title"] = title
    return output


def _result_item_from_payload(payload: Any) -> dict[str, str | None] | None:
    if not isinstance(payload, dict):
        return None
    title = _string_value(payload.get("title")).strip()
    url = _string_value(payload.get("url")).strip()
    if not title or not url:
        return None
    snippet = _string_value(
        payload.get("content") or payload.get("snippet") or payload.get("description")
    ).strip()
    source = _string_value(payload.get("source")).strip() or _host_from_url(url)
    published_at = _string_value(
        payload.get("published_at")
        or payload.get("published_date")
        or payload.get("date")
    ).strip()
    return {
        "title": title,
        "url": url,
        "snippet": snippet,
        "source": source or None,
        "published_at": published_at or None,
    }


def _auth_error(
    headers: Mapping[str, str], config: RelayConfig
) -> tuple[int, str, str] | None:
    authorization = _header_value(headers, "Authorization")
    if not authorization or not authorization.startswith("Bearer "):
        return (
            HTTPStatus.UNAUTHORIZED,
            "provider_auth_failed",
            "missing relay bearer token.",
        )
    token = authorization.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(token, config.relay_api_key):
        return (
            HTTPStatus.FORBIDDEN,
            "provider_permission_denied",
            "invalid relay bearer token.",
        )
    return None


def _json_object_from_body(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RelayRequestError(
            "provider_request_invalid", "relay request body must be valid JSON."
        ) from exc
    if not isinstance(payload, dict):
        raise RelayRequestError(
            "provider_request_invalid", "relay request body must be a JSON object."
        )
    return payload


def _search_error_payload(
    *,
    query_used: str,
    code: str,
    message: object,
    recoverable: bool | None,
) -> dict[str, Any]:
    error = build_provider_error(
        code,
        message,
        recoverable=recoverable,
        provider="tavily",
        capability="web_search",
    )
    return {
        "provider": "tavily",
        "query_used": query_used or "web_search",
        "results": [],
        "total": 0,
        "errors": [
            {
                "code": error.code,
                "message": error.message,
                "recoverable": error.recoverable,
            }
        ],
    }


def _fetch_error_payload(
    *,
    url: str,
    code: str,
    message: object,
    recoverable: bool | None,
    content_format: str,
) -> dict[str, Any]:
    error = build_provider_error(
        code,
        message,
        recoverable=recoverable,
        provider="tavily",
        capability="web_fetch",
    )
    return {
        "provider": "tavily",
        "url": url or "web_fetch",
        "content": "",
        "content_format": content_format if content_format in ALLOWED_EXTRACT_FORMATS else "markdown",
        "total_chars": 0,
        "truncated": False,
        "errors": [
            {
                "code": error.code,
                "message": error.message,
                "recoverable": error.recoverable,
            }
        ],
    }


def _http_error_message(exc: urllib.error.HTTPError, *, operation: str) -> str:
    detail = ""
    try:
        raw_body = exc.read().decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover - best effort diagnostics only
        raw_body = ""
    if raw_body:
        detail = _extract_error_detail(raw_body)
    return detail or f"Tavily {operation} returned HTTP {exc.code}."


def _extract_error_detail(raw_body: str) -> str:
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return raw_body
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, dict):
            nested = detail.get("error") or detail.get("message")
            if isinstance(nested, str):
                return nested
        for key in ("error", "message"):
            value = payload.get(key)
            if isinstance(value, str):
                return value
    return raw_body


def _http_error_code(status_code: int) -> str:
    if status_code == 401:
        return "provider_auth_failed"
    if status_code in {403, 432, 433}:
        return "provider_permission_denied"
    if status_code == 429:
        return "provider_rate_limited"
    if status_code == 408:
        return "provider_timeout"
    if status_code in {502, 503, 504}:
        return "provider_unavailable"
    if 500 <= status_code < 600:
        return "provider_bad_gateway"
    return "provider_request_invalid"


def _domain_from_site_filter(value: Any) -> str | None:
    text = _string_value(value).strip()
    if not text:
        return None
    if text.startswith("site:"):
        text = text.removeprefix("site:").strip()
    parsed = urlparse(text if "://" in text else f"//{text}")
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host or any(char.isspace() for char in host):
        return None
    return host.removeprefix("*.")


def _extract_url_from_payload(payload: Mapping[str, Any]) -> str:
    url = _string_value(payload.get("url")).strip()
    if not url:
        raise RelayRequestError(
            "provider_request_invalid", "web_fetch relay requires non-empty url.", "web_fetch"
        )
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RelayRequestError(
            "provider_request_invalid", "web_fetch relay requires http or https url.", url
        )
    return url


def _extract_format_from_payload(
    payload: Mapping[str, Any], config: RelayConfig
) -> str:
    value = _string_value(payload.get("content_format") or payload.get("format")).strip()
    if not value:
        return config.extract_format
    if value not in ALLOWED_EXTRACT_FORMATS:
        raise RelayRequestError(
            "provider_request_invalid",
            "web_fetch relay content_format must be markdown or text.",
            _string_value(payload.get("url")).strip() or "web_fetch",
        )
    return value


def _first_extract_result(results: Sequence[Any]) -> Mapping[str, Any] | None:
    for item in results:
        if not isinstance(item, dict):
            continue
        content = _string_value(
            item.get("raw_content") or item.get("content") or item.get("text")
        ).strip()
        if content:
            return item
    return None


def _failed_extract_message(value: Any) -> str:
    if not isinstance(value, list) or not value:
        return ""
    first = value[0]
    if not isinstance(first, dict):
        return ""
    for key in ("error", "message", "reason"):
        message = first.get(key)
        if isinstance(message, str) and message.strip():
            return message.strip()
    return ""


def _time_range_from_recency(value: Any) -> str | None:
    try:
        days = int(value)
    except (TypeError, ValueError):
        return None
    if days <= 0:
        return None
    if days <= 1:
        return "day"
    if days <= 7:
        return "week"
    if days <= 31:
        return "month"
    if days <= 365:
        return "year"
    return None


def _bounded_int(value: Any, *, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return min(high, max(low, parsed))


def _float_env(value: str | None, default: float) -> float:
    if not value:
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError("TAVILY_TIMEOUT_SECONDS must be a number.") from exc
    if parsed <= 0:
        raise ValueError("TAVILY_TIMEOUT_SECONDS must be greater than 0.")
    return parsed


def _header_value(headers: Mapping[str, str], name: str) -> str | None:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def _string_value(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _host_from_url(value: str) -> str | None:
    return urlparse(value).hostname


def _output_ref(payload: Mapping[str, Any], query_used: str) -> str:
    request_id = _string_value(payload.get("request_id")).strip()
    if request_id:
        return f"tavily://search/{request_id}"
    return f"tavily://search/{_slugify(query_used)}"


def _extract_output_ref(payload: Mapping[str, Any], url: str) -> str:
    request_id = _string_value(payload.get("request_id")).strip()
    if request_id:
        return f"tavily://extract/{request_id}"
    return f"tavily://extract/{_slugify(url)}"


def _slugify(value: str) -> str:
    chars = [
        char.lower() if char.isalnum() else "-" for char in value if char.isascii()
    ]
    slug = "-".join(part for part in "".join(chars).split("-") if part)
    return slug[:60] or "query"


def _normalize_path(value: str) -> str:
    stripped = value.strip() or DEFAULT_PATH
    return stripped if stripped.startswith("/") else f"/{stripped}"


def _fetch_path_for_search_path(value: str) -> str:
    path = _normalize_path(value).rstrip("/") or DEFAULT_PATH
    parts = [part for part in path.split("/") if part]
    if parts and parts[-1] == "search":
        parts[-1] = "fetch"
        return "/" + "/".join(parts)
    if parts and parts[-1] == "fetch":
        return "/" + "/".join(parts)
    return f"{path}/fetch"


if __name__ == "__main__":
    raise SystemExit(main())
