"""Volcengine Ark vision understanding adapter using Responses API."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from assistant_agent.media.vision.models import VisualUnderstandingResult
from assistant_agent.providers.provider_errors import ProviderAdapterError, sanitize_error_message
from assistant_agent.media.vision.real_vision_adapter import _json_object_from_text, map_vision_result
from assistant_agent.media.vision.vision_adapter import VisionUnderstandingInput


DEFAULT_ARK_VISION_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_ARK_VISION_MODEL = "doubao-seed-2-0-lite-260215"


@dataclass(frozen=True)
class ArkVisionProviderConfig:
    """Configuration for Ark Responses API vision understanding."""

    provider: str = "ark"
    api_key: str | None = None
    base_url: str = DEFAULT_ARK_VISION_BASE_URL
    model: str = DEFAULT_ARK_VISION_MODEL


class ArkVisionProviderAdapter:
    """Ark SDK adapter for image understanding with local file and multi-image support."""

    def __init__(self, config: ArkVisionProviderConfig) -> None:
        self.config = config

    def understand(self, input: VisionUnderstandingInput) -> VisualUnderstandingResult:
        if not self.config.api_key:
            raise ProviderAdapterError(
                "provider_unconfigured",
                "ark vision provider requires ARK_API_KEY (legacy ARK_VISION_API_KEY is also accepted).",
            )
        if not input.image_ids:
            raise ValueError("Ark vision provider requires image_ids.")
        if input.video_ids:
            raise ValueError("Ark vision provider currently supports image inputs only.")

        client = _create_ark_client(api_key=self.config.api_key, base_url=self.config.base_url)
        payload = build_ark_vision_input(input)
        try:
            response = client.responses.create(model=self.config.model, input=payload)
        except ProviderAdapterError:
            raise
        except Exception as exc:
            raise ProviderAdapterError("provider_bad_response", sanitize_error_message(exc)) from exc

        text = extract_ark_response_text(response)
        try:
            return map_vision_result(_json_object_from_text(text))
        except ValueError as exc:
            raise ProviderAdapterError("provider_bad_response", str(exc)) from exc


def build_ark_vision_input(input: VisionUnderstandingInput) -> list[dict[str, Any]]:
    """Build Ark Responses API input with one or more images."""

    content = [{"type": "input_image", "image_url": ark_image_url(image_ref)} for image_ref in input.image_ids]
    content.append({"type": "input_text", "text": _ark_vision_prompt(input.question)})
    return [{"role": "user", "content": content}]


def ark_image_url(image_ref: str) -> str:
    """Return Ark-compatible image URL, using file:// for local files."""

    if image_ref.startswith(("http://", "https://", "file://")):
        return image_ref
    path = Path(image_ref)
    if not path.exists() or not path.is_file():
        raise ValueError(f"图片文件不存在: {image_ref}")
    return f"file://{path.resolve()}"


def extract_ark_response_text(response: Any) -> str:
    """Extract text from Ark SDK Responses API objects or dicts."""

    output_text = _get_value(response, "output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text
    output = _get_value(response, "output")
    text = _find_text(output)
    if text:
        return text
    raise ProviderAdapterError("provider_bad_response", "Ark response did not include output text")


def _create_ark_client(*, api_key: str, base_url: str):
    try:
        from volcenginesdkarkruntime import Ark
    except ImportError as exc:
        raise ProviderAdapterError(
            "provider_unconfigured",
            "volcenginesdkarkruntime is required for ark vision provider.",
        ) from exc
    return Ark(base_url=base_url, api_key=api_key)


def _ark_vision_prompt(question: str | None) -> str:
    base_question = question or "请描述图片中的主要物体、颜色、材质和场景。"
    return (
        f"{base_question}\n"
        "请只输出一个 JSON object，字段必须符合："
        "objects: string[], colors: string[], materials: string[], scene: string | null, "
        "style_tags: string[], text_in_media: string[], summary: string。"
    )


def _find_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value if value.strip() else None
    if isinstance(value, list):
        for item in value:
            text = _find_text(item)
            if text:
                return text
    if isinstance(value, dict):
        for key in ("text", "output_text"):
            text = value.get(key)
            if isinstance(text, str) and text.strip():
                return text
        for key in ("content", "message", "output"):
            text = _find_text(value.get(key))
            if text:
                return text
    if hasattr(value, "__dict__"):
        return _find_text(vars(value))
    return None


def _get_value(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)
