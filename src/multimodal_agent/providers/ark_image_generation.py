"""Volcengine Ark image generation provider adapter."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from multimodal_agent.schemas.generation import ImageGenerationInput, ImageGenerationResult
from multimodal_agent.services.provider_errors import ProviderAdapterError, sanitize_error_message
from multimodal_agent.utils.prompting import build_image_prompt


DEFAULT_ARK_IMAGE_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_ARK_IMAGE_MODEL = "doubao-seedream-5-0-260128"
DEFAULT_ARK_IMAGE_SIZE = "2K"
DEFAULT_ARK_IMAGE_OUTPUT_FORMAT = "png"


@dataclass(frozen=True)
class ArkImageGenerationConfig:
    """Configuration for the optional Ark image generation adapter."""

    api_key: str | None
    base_url: str = DEFAULT_ARK_IMAGE_BASE_URL
    model: str = DEFAULT_ARK_IMAGE_MODEL
    default_size: str = DEFAULT_ARK_IMAGE_SIZE
    output_format: str = DEFAULT_ARK_IMAGE_OUTPUT_FORMAT
    timeout_seconds: float = 120.0


class ArkImageGenerationAdapter:
    """HTTP adapter for Volcengine Ark OpenAI-compatible image generation."""

    provider = "ark"

    def __init__(self, config: ArkImageGenerationConfig) -> None:
        self.config = config

    def generate(self, input: ImageGenerationInput) -> ImageGenerationResult:
        """Generate images through Ark and return the stable generation schema."""

        if not self.config.api_key:
            raise ProviderAdapterError("provider_unconfigured", "ark image provider requires ARK_IMAGE_API_KEY")
        _validate_http_header_value("Authorization", f"Bearer {self.config.api_key}")

        prompt = build_image_prompt(input)
        payload = build_ark_image_payload(
            prompt=prompt,
            model=self.config.model,
            size=normalize_ark_image_size(input.size or self.config.default_size, width=input.width, height=input.height),
            output_format=self.config.output_format,
            watermark=input.watermark,
        )
        request = urllib.request.Request(
            ark_image_generation_url(self.config.base_url),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                response_data = json.loads(response.read().decode("utf-8"))
                status = getattr(response, "status", 200)
        except TimeoutError as exc:
            raise ProviderAdapterError("provider_timeout", str(exc)) from exc
        except urllib.error.HTTPError as exc:
            raise _provider_error_from_http_error(exc) from exc
        except urllib.error.URLError as exc:
            raise ProviderAdapterError("provider_unavailable", str(exc.reason)) from exc
        except json.JSONDecodeError as exc:
            raise ProviderAdapterError("provider_bad_response", "response JSON decode failed") from exc

        _raise_for_provider_error(response_data, status=status)
        image_urls = parse_ark_image_urls(response_data)
        if not image_urls:
            raise ProviderAdapterError("provider_empty_response", "Ark response did not include image URLs")

        request_id = _request_id(response_data)
        return ImageGenerationResult(
            task_id=request_id or "ark_image_generation",
            status="succeeded",
            image_url=image_urls[0],
            image_urls=image_urls,
            request_id=request_id,
            prompt=prompt,
            provider=self.provider,
            model=self.config.model,
            output_ref=image_urls[0],
            prompt_used=prompt,
            raw=response_data,
        )


def build_ark_image_payload(
    *,
    prompt: str,
    model: str = DEFAULT_ARK_IMAGE_MODEL,
    size: str = DEFAULT_ARK_IMAGE_SIZE,
    output_format: str = DEFAULT_ARK_IMAGE_OUTPUT_FORMAT,
    watermark: bool = False,
) -> dict[str, Any]:
    """Build Ark OpenAI-compatible image generation payload."""

    return {
        "model": model,
        "prompt": prompt,
        "size": size,
        "output_format": output_format,
        "response_format": "url",
        "extra_body": {"watermark": watermark},
    }


def normalize_ark_image_size(size: str | None, *, width: int | None = None, height: int | None = None) -> str:
    """Normalize common LLM pixel formats to Ark Seedream size tokens."""

    if width is not None and height is not None:
        return _pixel_size_to_ark_token(width, height)
    candidate = (size or DEFAULT_ARK_IMAGE_SIZE).strip()
    lowered = candidate.lower().replace(" ", "")
    if lowered in {"1k", "2k", "4k"}:
        return lowered.upper()
    separator = "x" if "x" in lowered else "*" if "*" in lowered else None
    if separator is not None:
        left, right = lowered.split(separator, 1)
        if left.isdigit() and right.isdigit():
            return _pixel_size_to_ark_token(int(left), int(right))
    return candidate


def _pixel_size_to_ark_token(width: int, height: int) -> str:
    pixels = width * height
    if pixels >= 4096 * 4096:
        return "4K"
    return "2K"


def ark_image_generation_url(base_url: str) -> str:
    """Return the Ark images generation endpoint for a base URL or full endpoint."""

    normalized = (base_url or DEFAULT_ARK_IMAGE_BASE_URL).rstrip("/")
    suffix = "/images/generations"
    if normalized.endswith(suffix):
        return normalized
    return f"{normalized}{suffix}"


def parse_ark_image_urls(data: dict[str, Any]) -> list[str]:
    """Extract image URLs from OpenAI-compatible image response data."""

    image_urls: list[str] = []
    for item in data.get("data") or []:
        if isinstance(item, dict) and isinstance(item.get("url"), str):
            image_urls.append(item["url"])
    return image_urls


def _validate_http_header_value(name: str, value: str) -> None:
    try:
        value.encode("latin-1")
    except UnicodeEncodeError as exc:
        raise ProviderAdapterError(
            "provider_invalid_config",
            f"{name} header contains non-latin-1 characters; check quotes and hidden characters in .env.",
        ) from exc


def _raise_for_provider_error(data: dict[str, Any], *, status: int) -> None:
    error = data.get("error") if isinstance(data.get("error"), dict) else {}
    code = data.get("code") or error.get("code")
    message = data.get("message") or error.get("message")
    if status >= 400 or code or message:
        request_id = _request_id(data)
        raise ProviderAdapterError(
            _http_status_to_error_code(status),
            _format_provider_error(status=status, code=code, message=message, request_id=request_id),
        )


def _provider_error_from_http_error(exc: urllib.error.HTTPError) -> ProviderAdapterError:
    body = _read_http_error_body(exc)
    error = body.get("error") if isinstance(body.get("error"), dict) else {}
    code = body.get("code") or error.get("code")
    message = body.get("message") or error.get("message") or f"HTTP {exc.code}"
    request_id = _request_id(body) or exc.headers.get("X-Request-Id")
    return ProviderAdapterError(
        _http_status_to_error_code(exc.code),
        _format_provider_error(status=exc.code, code=code, message=message, request_id=request_id),
    )


def _read_http_error_body(exc: urllib.error.HTTPError) -> dict[str, Any]:
    try:
        raw = exc.read().decode("utf-8")
        parsed = json.loads(raw)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _format_provider_error(
    *,
    status: int,
    code: object | None,
    message: object | None,
    request_id: object | None,
) -> str:
    parts = [f"status={status}"]
    if code:
        parts.append(f"code={sanitize_error_message(code)}")
    if message:
        parts.append(f"message={sanitize_error_message(message)}")
    if request_id:
        parts.append(f"request_id={sanitize_error_message(request_id)}")
    return ", ".join(parts)


def _request_id(data: dict[str, Any]) -> str | None:
    value = data.get("request_id") or data.get("id")
    return value if isinstance(value, str) else None


def _http_status_to_error_code(status: int) -> str:
    if status == 401:
        return "provider_auth_failed"
    if status == 403:
        return "provider_permission_denied"
    if status == 429:
        return "provider_rate_limited"
    if status >= 500:
        return "provider_bad_gateway"
    if status >= 400:
        return "provider_bad_response"
    return "provider_execution_failed"
