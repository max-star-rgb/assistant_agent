"""DashScope Qwen image generation provider adapter."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from multimodal_agent.schemas.generation import ImageGenerationResult
from multimodal_agent.services.image_generation_adapter import ImageGenerationInput, build_image_prompt
from multimodal_agent.services.provider_errors import ProviderAdapterError, sanitize_error_message


DEFAULT_QWEN_IMAGE_BASE_URL = "https://dashscope.aliyuncs.com/api/v1"
DEFAULT_QWEN_IMAGE_MODEL = "qwen-image-2.0-pro"
DEFAULT_QWEN_IMAGE_SIZE = "1024*1024"
DEFAULT_NEGATIVE_PROMPT = "低分辨率，低画质，肢体畸形，手指畸形，文字模糊，构图混乱"


@dataclass(frozen=True)
class QwenImageGenerationConfig:
    """Configuration for the optional DashScope Qwen image generation adapter."""

    api_key: str | None
    base_url: str = DEFAULT_QWEN_IMAGE_BASE_URL
    model: str = DEFAULT_QWEN_IMAGE_MODEL
    default_size: str = DEFAULT_QWEN_IMAGE_SIZE
    timeout_seconds: float = 60.0


class QwenImageGenerationAdapter:
    """HTTP adapter for DashScope Qwen text-to-image generation."""

    provider = "qwen"

    def __init__(self, config: QwenImageGenerationConfig) -> None:
        self.config = config

    def generate(self, input: ImageGenerationInput) -> ImageGenerationResult:
        """Generate images through DashScope and return the stable generation schema."""

        if not self.config.api_key:
            raise ProviderAdapterError("provider_unconfigured", "qwen image provider requires QWEN_IMAGE_API_KEY")

        prompt = build_image_prompt(input)
        payload = build_qwen_image_payload(
            prompt=prompt,
            model=self.config.model,
            size=normalize_qwen_image_size(input.size or self.config.default_size, width=input.width, height=input.height),
            n=input.n,
            negative_prompt=input.negative_prompt,
            prompt_extend=input.prompt_extend,
            watermark=input.watermark,
            seed=input.seed,
        )
        request = urllib.request.Request(
            qwen_image_generation_url(self.config.base_url),
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
        image_urls = parse_qwen_image_urls(response_data)
        if not image_urls:
            raise ProviderAdapterError("provider_empty_response", "DashScope response did not include image URLs")

        request_id = _request_id(response_data)
        return ImageGenerationResult(
            task_id=request_id or "qwen_image_generation",
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


def build_qwen_image_payload(
    *,
    prompt: str,
    model: str = DEFAULT_QWEN_IMAGE_MODEL,
    size: str = DEFAULT_QWEN_IMAGE_SIZE,
    n: int = 1,
    negative_prompt: str | None = None,
    prompt_extend: bool = True,
    watermark: bool = False,
    seed: int | None = None,
) -> dict[str, Any]:
    """Build DashScope multimodal-generation request payload."""

    parameters: dict[str, Any] = {
        "size": size,
        "n": n,
        "prompt_extend": prompt_extend,
        "watermark": watermark,
        "negative_prompt": negative_prompt or DEFAULT_NEGATIVE_PROMPT,
    }
    if seed is not None:
        parameters["seed"] = seed
    return {
        "model": model,
        "input": {
            "messages": [
                {
                    "role": "user",
                    "content": [{"text": prompt}],
                }
            ]
        },
        "parameters": parameters,
    }


def normalize_qwen_image_size(size: str | None, *, width: int | None = None, height: int | None = None) -> str:
    """Normalize common image size formats to DashScope's width*height format."""

    if width is not None and height is not None:
        return f"{width}*{height}"
    candidate = (size or DEFAULT_QWEN_IMAGE_SIZE).strip().lower()
    if "x" in candidate and "*" not in candidate:
        left, right = candidate.split("x", 1)
        if left.strip().isdigit() and right.strip().isdigit():
            return f"{left.strip()}*{right.strip()}"
    return candidate


def qwen_image_generation_url(base_url: str) -> str:
    """Return the DashScope image generation endpoint for a base URL or full endpoint."""

    normalized = (base_url or DEFAULT_QWEN_IMAGE_BASE_URL).rstrip("/")
    suffix = "/services/aigc/multimodal-generation/generation"
    if normalized.endswith(suffix):
        return normalized
    return f"{normalized}{suffix}"


def parse_qwen_image_urls(data: dict[str, Any]) -> list[str]:
    """Extract image URLs from DashScope output choices."""

    output = data.get("output") or {}
    choices = output.get("choices") or []
    image_urls: list[str] = []
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message") or {}
        content = message.get("content") or []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("image"), str):
                image_urls.append(item["image"])
    return image_urls


def _raise_for_provider_error(data: dict[str, Any], *, status: int) -> None:
    code = data.get("code")
    message = data.get("message")
    if status >= 400 or code or message:
        request_id = _request_id(data)
        raise ProviderAdapterError(
            _http_status_to_error_code(status),
            _format_provider_error(status=status, code=code, message=message, request_id=request_id),
        )


def _provider_error_from_http_error(exc: urllib.error.HTTPError) -> ProviderAdapterError:
    body = _read_http_error_body(exc)
    code = body.get("code")
    message = body.get("message") or f"HTTP {exc.code}"
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
    value = data.get("request_id") or data.get("requestId") or (data.get("output") or {}).get("request_id")
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
