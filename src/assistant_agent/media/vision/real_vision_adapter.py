"""Optional real vision provider adapter implementations."""

from __future__ import annotations

import base64
import mimetypes
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from assistant_agent.media.vision.models import VisualUnderstandingResult
from assistant_agent.providers.provider_errors import ProviderAdapterError
from assistant_agent.media.vision.vision_adapter import VisionUnderstandingInput


@dataclass(frozen=True)
class RealVisionProviderConfig:
    """Configuration required by an optional real vision provider."""

    provider: str
    api_key: str | None
    base_url: str
    model: str


class HttpVisionProviderAdapter:
    """HTTP-based optional vision adapter.

    This adapter is not used by default. It exists so real providers can be
    enabled explicitly by environment configuration and integration tests.
    """

    def __init__(self, config: RealVisionProviderConfig, timeout_seconds: float = 10.0) -> None:
        self.config = config
        self.timeout_seconds = timeout_seconds

    def understand(self, input: VisionUnderstandingInput) -> VisualUnderstandingResult:
        if not self.config.api_key:
            raise ProviderAdapterError(
                "provider_unconfigured",
                f"{self.config.provider} vision provider requires an API key",
            )
        if not input.image_ids and not input.video_ids:
            raise ValueError("缺少图片或视频 ID，无法进行视觉理解")

        payload = build_openai_vision_payload(input, self.config.model)
        request = urllib.request.Request(
            chat_completions_url(self.config.base_url),
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
        except TimeoutError as exc:
            raise ProviderAdapterError("provider_timeout", str(exc)) from exc
        except urllib.error.HTTPError as exc:
            raise ProviderAdapterError(_http_error_code(exc.code), f"HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise ProviderAdapterError("provider_unavailable", str(exc.reason)) from exc
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ProviderAdapterError("provider_bad_response", str(exc)) from exc

        try:
            return parse_openai_vision_response(data)
        except ValueError as exc:
            raise ProviderAdapterError("provider_bad_response", str(exc)) from exc


class DashScopeVisionProviderAdapter:
    """DashScope-native multimodal image understanding adapter."""

    def __init__(
        self, config: RealVisionProviderConfig, timeout_seconds: float = 10.0
    ) -> None:
        self.config = config
        self.timeout_seconds = timeout_seconds

    def understand(self, input: VisionUnderstandingInput) -> VisualUnderstandingResult:
        if not self.config.api_key:
            raise ProviderAdapterError(
                "provider_unconfigured",
                "qwen vision provider requires an API key",
            )
        if not input.image_ids:
            raise ValueError("缺少图片 ID，无法进行视觉理解")
        if input.video_ids:
            raise ValueError("DashScope 原生视觉 adapter 只接受有序图片输入")

        request = urllib.request.Request(
            dashscope_multimodal_url(self.config.base_url),
            data=json.dumps(
                build_dashscope_vision_payload(input, self.config.model)
            ).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds
            ) as response:
                data = json.loads(response.read().decode("utf-8"))
        except TimeoutError as exc:
            raise ProviderAdapterError("provider_timeout", str(exc)) from exc
        except urllib.error.HTTPError as exc:
            raise ProviderAdapterError(
                _http_error_code(exc.code), f"HTTP {exc.code}"
            ) from exc
        except urllib.error.URLError as exc:
            raise ProviderAdapterError("provider_unavailable", str(exc.reason)) from exc
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ProviderAdapterError("provider_bad_response", str(exc)) from exc

        try:
            return parse_dashscope_vision_response(data)
        except ValueError as exc:
            raise ProviderAdapterError("provider_bad_response", str(exc)) from exc


def chat_completions_url(base_url: str) -> str:
    """Return a chat completions endpoint from a provider base URL or endpoint."""

    normalized = base_url.rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    return f"{normalized}/chat/completions"


def dashscope_multimodal_url(base_url: str) -> str:
    """Return the native DashScope multimodal generation endpoint."""

    normalized = base_url.rstrip("/")
    endpoint = "/api/v1/services/aigc/multimodal-generation/generation"
    if normalized.endswith(endpoint):
        return normalized
    scheme, separator, remainder = normalized.partition("://")
    if not separator or not scheme or not remainder:
        raise ValueError("DashScope vision base URL must be absolute")
    host = remainder.split("/", maxsplit=1)[0]
    return f"{scheme}://{host}{endpoint}"


def build_openai_vision_payload(input: VisionUnderstandingInput, model: str) -> dict[str, Any]:
    """Build OpenAI-compatible Chat Completions payload for image understanding."""

    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": _vision_prompt(input.question),
        }
    ]
    for image_id in input.image_ids:
        content.append({"type": "image_url", "image_url": {"url": image_to_data_url(image_id)}})
    if input.video_ids:
        raise ValueError("真实 OpenAI-compatible smoke 暂不支持视频输入，请先使用图片")

    return {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "response_format": {"type": "json_object"},
    }


def build_dashscope_vision_payload(
    input: VisionUnderstandingInput,
    model: str,
) -> dict[str, Any]:
    """Build a DashScope-native ordered multi-image request."""

    if input.video_ids:
        raise ValueError("DashScope 原生视觉 adapter 只接受有序图片输入")
    content = [{"image": image_to_data_url(image_id)} for image_id in input.image_ids]
    content.append({"text": _vision_prompt(input.question)})
    return {
        "model": model,
        "input": {"messages": [{"role": "user", "content": content}]},
        "parameters": {
            "enable_thinking": False,
            "result_format": "message",
            "temperature": 0,
        },
    }


def parse_openai_vision_response(data: dict[str, Any]) -> VisualUnderstandingResult:
    """Parse OpenAI-compatible response into VisualUnderstandingResult."""

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("missing choices[0].message.content") from exc
    if isinstance(content, list):
        text_parts = [part.get("text", "") for part in content if isinstance(part, dict)]
        content = "\n".join(part for part in text_parts if part)
    if not isinstance(content, str):
        raise ValueError("response message content is not text")
    parsed = _json_object_from_text(content)
    return map_vision_result(parsed)


def parse_dashscope_vision_response(data: dict[str, Any]) -> VisualUnderstandingResult:
    """Parse a DashScope-native multimodal response."""

    try:
        content = data["output"]["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("missing output.choices[0].message.content") from exc
    if isinstance(content, list):
        content = "\n".join(
            item.get("text", "") for item in content if isinstance(item, dict)
        )
    if not isinstance(content, str):
        raise ValueError("response message content is not text")
    return map_vision_result(_json_object_from_text(content))


def map_vision_result(parsed: dict[str, Any]) -> VisualUnderstandingResult:
    """Normalize provider JSON into the stable VisualUnderstandingResult schema."""

    if not parsed:
        raise ValueError("response JSON is empty")
    normalized = {
        "objects": _string_list(parsed.get("objects"), "objects"),
        "colors": _string_list(parsed.get("colors"), "colors"),
        "materials": _string_list(parsed.get("materials"), "materials"),
        "scene": _optional_string(parsed.get("scene"), "scene"),
        "style_tags": _string_list(parsed.get("style_tags"), "style_tags"),
        "text_in_media": _string_list(parsed.get("text_in_media"), "text_in_media"),
        "summary": _summary(parsed.get("summary"), parsed),
    }
    return VisualUnderstandingResult.model_validate(normalized)


def image_to_data_url(image_ref: str) -> str:
    """Return an image URL or encode a local image path as a data URL."""

    if image_ref.startswith(("http://", "https://", "data:")):
        return image_ref

    path = Path(image_ref)
    if not path.exists() or not path.is_file():
        raise ValueError(f"图片文件不存在: {image_ref}")
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _vision_prompt(question: str | None) -> str:
    base_question = question or "请描述图片中的主要物体、颜色、材质和场景。"
    return (
        f"{base_question}\n"
        "请只输出一个 JSON object，字段必须符合："
        "objects: string[], colors: string[], materials: string[], scene: string | null, "
        "style_tags: string[], text_in_media: string[], summary: string。"
    )


def _json_object_from_text(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        parsed = json.loads(stripped[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("response JSON is not an object")
    return parsed


def _string_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list")
    result = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{field_name} items must be strings")
        result.append(item)
    return result


def _optional_string(value: Any, field_name: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    return value


def _summary(value: Any, parsed: dict[str, Any]) -> str:
    if value is None:
        objects = parsed.get("objects")
        if isinstance(objects, list) and objects and all(isinstance(item, str) for item in objects):
            return f"视觉结果包含：{'、'.join(objects)}。"
        return "真实视觉 Provider 返回了结构化结果。"
    if not isinstance(value, str):
        raise ValueError("summary must be a string")
    if not value.strip():
        return "真实视觉 Provider 返回了结构化结果。"
    return value


def _http_error_code(status_code: int) -> str:
    if status_code in {401, 403}:
        return "provider_auth_failed"
    if status_code == 429:
        return "provider_rate_limited"
    if 500 <= status_code:
        return "provider_unavailable"
    return "provider_bad_response"
