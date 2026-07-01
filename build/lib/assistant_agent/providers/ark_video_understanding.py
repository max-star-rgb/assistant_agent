"""Volcengine Ark adapter for frame-based video understanding."""

from __future__ import annotations

import time
import os
import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from assistant_agent.providers.ark_vision import ark_image_url, extract_ark_response_text
from assistant_agent.schemas.perception import VideoUnderstandingRequest, VideoUnderstandingResult
from assistant_agent.services.provider_errors import ProviderAdapterError, sanitize_error_message
from assistant_agent.services.real_vision_adapter import _json_object_from_text
from assistant_agent.services.video_adapter import _failed_result


DEFAULT_ARK_VIDEO_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_ARK_VIDEO_MODEL = "doubao-seed-2-0-lite-260215"


@dataclass(frozen=True)
class ArkVideoUnderstandingConfig:
    """Configuration for Ark Responses API video-frame understanding."""

    provider: str = "ark"
    api_key: str | None = None
    base_url: str = DEFAULT_ARK_VIDEO_BASE_URL
    model: str = DEFAULT_ARK_VIDEO_MODEL


class ArkVideoUnderstandingAdapter:
    """Use Ark Responses API over recent video frames supplied as multiple images."""

    provider = "ark"

    def __init__(self, config: ArkVideoUnderstandingConfig) -> None:
        self.config = config

    def understand_video(self, request: VideoUnderstandingRequest) -> VideoUnderstandingResult:
        started_at = time.perf_counter()
        if not request.video_ref:
            raise ValueError("video_missing_input: VideoUnderstandingRequest requires video_ref.")
        if not self.config.api_key:
            return _failed_result(
                provider=self.provider,
                model=self.config.model,
                code="provider_unconfigured",
                message="ark video provider is missing ARK_VISION_API_KEY.",
                recoverable=True,
            )
        if not request.frame_refs:
            return _failed_result(
                provider=self.provider,
                model=self.config.model,
                code="video_missing_frames",
                message="ark video provider requires frame_refs from the video context window.",
                recoverable=True,
            )

        try:
            with _without_invalid_socks_proxy_env():
                response = asyncio.run(
                    _call_ark_responses(
                        api_key=self.config.api_key,
                        base_url=self.config.base_url,
                        model=self.config.model,
                        input=build_ark_video_input(request),
                    )
                )
            text = extract_ark_response_text(response)
            result = map_ark_video_result(
                _json_object_from_text(text),
                provider=self.provider,
                model=self.config.model,
                video_ref=request.video_ref,
                latency_ms=int((time.perf_counter() - started_at) * 1000),
            )
            return result
        except ProviderAdapterError as exc:
            return _failed_result(
                provider=self.provider,
                model=self.config.model,
                code=exc.code,
                message=exc.message,
                recoverable=False,
            )
        except Exception as exc:
            return _failed_result(
                provider=self.provider,
                model=self.config.model,
                code="provider_bad_response",
                message=_ark_video_error_message(
                    exc,
                    model=self.config.model,
                    base_url=self.config.base_url,
                ),
                recoverable=False,
            )


def build_ark_video_input(request: VideoUnderstandingRequest) -> list[dict[str, Any]]:
    """Build Ark Responses API input from recent video frames."""

    content: list[dict[str, Any]] = [
        {"type": "input_image", "image_url": ark_image_url(frame_ref)}
        for frame_ref in request.frame_refs
    ]
    content.append({"type": "input_text", "text": _ark_video_prompt(request)})
    return [{"role": "user", "content": content}]


def map_ark_video_result(
    data: dict[str, Any],
    *,
    provider: str,
    model: str,
    video_ref: str,
    latency_ms: int,
) -> VideoUnderstandingResult:
    """Map provider JSON into the stable video understanding result."""

    return VideoUnderstandingResult(
        summary=_string(data.get("summary")) or "已完成视频帧理解。",
        objects=_string_list(data.get("objects")),
        actions=_string_list(data.get("actions")),
        events=_string_list(data.get("events")),
        scene=_string(data.get("scene")),
        products=_string_list(data.get("products")),
        brands=_string_list(data.get("brands")),
        colors=_string_list(data.get("colors")),
        materials=_string_list(data.get("materials")),
        text_in_video=_string_list(data.get("text_in_video") or data.get("text_in_media")),
        timestamps=_dict_list(data.get("timestamps")),
        style_tags=_string_list(data.get("style_tags")),
        confidence=_confidence(data.get("confidence")),
        provider=provider,
        model=model,
        output_ref=f"provider://video/{provider}/{_safe_ref_suffix(video_ref)}",
        errors=[],
        latency_ms=latency_ms,
    )


async def _call_ark_responses(
    *,
    api_key: str,
    base_url: str,
    model: str,
    input: list[dict[str, Any]],
) -> Any:
    client = _create_ark_client(api_key=api_key, base_url=base_url)
    return await client.responses.create(model=model, input=input)


def _create_ark_client(*, api_key: str, base_url: str):
    try:
        from volcenginesdkarkruntime import AsyncArk
    except ImportError as exc:
        raise ProviderAdapterError(
            "provider_unconfigured",
            "volcenginesdkarkruntime is required for ark video provider.",
        ) from exc
    return AsyncArk(base_url=base_url, api_key=api_key)


def _ark_video_error_message(exc: Exception, *, model: str, base_url: str) -> str:
    """Build a diagnostic but redacted Ark video error message."""

    raw = sanitize_error_message(str(exc))
    request_id = _get_attr(exc, "request_id") or _request_id_from_text(raw)
    status_code = _get_attr(exc, "status_code")
    code = _get_attr(exc, "code")
    body = _get_attr(exc, "body")
    parts = [f"model={model}", f"base_url={base_url}"]
    if status_code is not None:
        parts.append(f"status={status_code}")
    if code:
        parts.append(f"code={code}")
    if request_id:
        parts.append(f"request_id={request_id}")
    if body:
        parts.append(f"body={sanitize_error_message(body)}")
    parts.append(f"{type(exc).__name__}: {raw}")
    return "; ".join(parts)


def _get_attr(value: Any, name: str) -> Any:
    try:
        return getattr(value, name)
    except Exception:
        return None


def _request_id_from_text(text: str) -> str | None:
    marker = "request_id:"
    if marker in text:
        return text.split(marker, 1)[1].split()[0].strip(",.;")
    marker = "Request id:"
    if marker in text:
        return text.split(marker, 1)[1].split()[0].strip(",.;")
    return None


@contextmanager
def _without_invalid_socks_proxy_env():
    """Temporarily remove socks:// proxy vars because httpx expects socks5/socks5h."""

    proxy_keys = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
    removed: dict[str, str] = {}
    for key in proxy_keys:
        value = os.environ.get(key)
        if isinstance(value, str) and value.lower().startswith("socks://"):
            removed[key] = value
            os.environ.pop(key, None)
    try:
        yield
    finally:
        os.environ.update(removed)


def _ark_video_prompt(request: VideoUnderstandingRequest) -> str:
    query = request.user_query or "请基于这些连续视频帧总结视频内容。"
    frame_count = len(request.frame_refs)
    return (
        f"{query}\n"
        f"这里有 {frame_count} 张按时间顺序排列的视频上下文帧。"
        "请只输出一个 JSON object，字段必须符合："
        "summary: string, objects: string[], actions: string[], events: string[], "
        "scene: string | null, products: string[], brands: string[], colors: string[], "
        "materials: string[], text_in_video: string[], timestamps: object[], "
        "style_tags: string[], confidence: number。"
    )


def _safe_ref_suffix(video_ref: str) -> str:
    suffix = video_ref.rsplit("/", maxsplit=1)[-1].strip() or "demo"
    return "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in suffix)


def _string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _confidence(value: Any) -> float | None:
    if isinstance(value, int | float):
        return max(0.0, min(1.0, float(value)))
    return None
