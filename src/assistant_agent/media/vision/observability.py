"""Content-safe native LangSmith tracing for VLM inference calls."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
import subprocess
from time import perf_counter
from typing import Any, TypeVar
from uuid import uuid4

from langchain_core.runnables import RunnableConfig
from langsmith import trace
from langsmith.schemas import Attachment
from langsmith.utils import tracing_is_enabled
from pydantic import BaseModel, ConfigDict


VISION_INFERENCE_OBSERVATION_NAME = "vlm.infer"
VISION_INFERENCE_PROMPT_VERSION = "vision-understanding-v1"
_ResultT = TypeVar("_ResultT")
_VLM_OUTPUT_FIELDS = (
    "summary",
    "scene",
    "objects",
    "people",
    "actions",
    "events",
    "changes",
    "uncertainties",
    "text_in_media",
    "text_in_video",
    "products",
    "brands",
    "colors",
    "materials",
    "style_tags",
    "timestamps",
    "confidence",
    "provider",
    "model",
    "latency_ms",
)
_MAX_VLM_CONTENT_TEXT_CHARS = 4_000
_MAX_VLM_CONTENT_ITEMS = 20
_MAX_KEYFRAME_BYTES = 8 * 1024 * 1024
_MAX_KEYFRAME_VIDEO_BYTES = 48 * 1024 * 1024
_KEYFRAME_VIDEO_TIMEOUT_SECONDS = 10.0
_VLM_INPUT_FIELDS = (
    "mode",
    "prompt_version",
    "resolved_instructions",
    "query",
    "media_kind",
    "frame_sequence",
    "visual_window_id",
    "window_start_sequence",
    "target_sequence",
    "window_role",
    "provider_connection_isolated",
    "frame_count",
    "history_frame_count",
    "memory_context_present",
)
_BLOCKED_VLM_CONTENT_KEYS = frozenset(
    {
        "evidence_ref",
        "frame_ref",
        "frame_refs",
        "image_id",
        "image_ids",
        "media_ref",
        "media_refs",
        "output_ref",
        "path",
        "provider_raw_response",
        "raw_provider_payload",
        "uri",
        "video_id",
        "video_ids",
    }
)


@dataclass
class VisionInferenceTraceContext:
    """Native root identity shared with one nested VLM generation."""

    trace_id: str
    run_id: str
    last_link: "VisionInferenceTraceLink | None" = None


class VisionInferenceTraceLink(BaseModel):
    """Prompt-safe identity of one VLM generation."""

    model_config = ConfigDict(frozen=True)

    trace_id: str
    run_id: str
    span_id: str


def trace_visual_observation(
    call: Callable[[VisionInferenceTraceContext | None], _ResultT],
    *,
    thread_id: str,
    frame_refs: Sequence[str],
    frame_sequences: Sequence[int],
    frame_timestamps_ms: Sequence[int | None],
    visual_window_id: str | None,
    window_start_sequence: int | None,
    target_sequence: int,
    window_role: str,
    provider_connection_isolated: bool,
    semantic_threshold: float,
    include_frame_attachments: bool = True,
) -> tuple[_ResultT, VisionInferenceTraceLink | None]:
    """Trace one closed keyframe window as an independent LangSmith root run."""

    if not tracing_is_enabled():
        return call(None), None
    metadata: dict[str, Any] = {
        "thread_id": thread_id,
        "trace_kind": "vision_observation",
        "target_sequence": target_sequence,
        "window_role": window_role,
        "provider_connection_isolated": provider_connection_isolated,
        "semantic_threshold": semantic_threshold,
    }
    if visual_window_id:
        metadata["visual_window_id"] = visual_window_id
    if window_start_sequence is not None:
        metadata["window_start_sequence"] = window_start_sequence
    called = False
    result: _ResultT
    business_error: BaseException | None = None
    context: VisionInferenceTraceContext | None = None
    try:
        with trace(
            "vision.observation",
            inputs={
                "frame_count": len(frame_sequences),
                "frame_sequences": list(frame_sequences),
                "frame_timestamps_ms": list(frame_timestamps_ms),
            },
            metadata=metadata,
            tags=["vision-observation"],
            parent="ignore",
            attachments=_visual_attachments(
                frame_refs,
                frame_sequences,
                include_frames=include_frame_attachments,
            ),
        ) as root:
            context = VisionInferenceTraceContext(
                trace_id=str(root.trace_id),
                run_id=str(root.id),
            )
            called = True
            try:
                result = call(context)
            except BaseException as exc:
                business_error = exc
                root.end(error="Visual observation failed.")
            else:
                root.end(outputs=_visual_observation_output(result))
    except Exception:
        if business_error is not None:
            raise business_error
        if called:
            return result, context.last_link if context is not None else None
        return call(None), None
    if business_error is not None:
        raise business_error
    return result, context.last_link if context is not None else None


def invoke_native_vision_model(
    call: Callable[[RunnableConfig], _ResultT],
    *,
    context: VisionInferenceTraceContext | None,
    capability: str,
    source: str,
    media_kind: str,
    media_count: int,
    trace_link_callback: Callable[[VisionInferenceTraceLink], None] | None = None,
    **metadata: Any,
) -> _ResultT:
    """Invoke one callback-native vision model with an exact preassigned run ID."""

    run_id = uuid4()
    config: RunnableConfig = {
        "run_name": VISION_INFERENCE_OBSERVATION_NAME,
        "run_id": run_id,
        "tags": ["vlm"],
        "metadata": _vision_inference_metadata(
            capability=capability,
            source=source,
            media_kind=media_kind,
            media_count=media_count,
            extra=metadata,
        ),
    }
    if context is not None:
        link = VisionInferenceTraceLink(
            trace_id=context.trace_id,
            run_id=context.run_id,
            span_id=str(run_id),
        )
        context.last_link = link
        _notify_trace_link_fail_open(trace_link_callback, link)
    return call(config)


def observe_vision_inference(
    call: Callable[[], _ResultT],
    *,
    context: object | None,
    capability: str,
    source: str,
    media_kind: str,
    media_count: int,
    frame_sequence: int | None = None,
    visual_window_id: str | None = None,
    window_start_sequence: int | None = None,
    target_sequence: int | None = None,
    window_role: str | None = None,
    provider_connection_isolated: bool | None = None,
    query_provided: bool | None = None,
    prompt_version: str = VISION_INFERENCE_PROMPT_VERSION,
    local_input_content: Mapping[str, Any] | None = None,
    trace_link_callback: Callable[[VisionInferenceTraceLink], None] | None = None,
) -> _ResultT:
    """Run one VLM call as a native child generation when tracing is active."""

    if not tracing_is_enabled():
        return call()
    common = _vision_inference_metadata(
        capability=capability,
        source=source,
        media_kind=media_kind,
        media_count=media_count,
        extra={
            "prompt_version": prompt_version,
            "frame_sequence": frame_sequence,
            "query_provided": query_provided,
            "visual_window_id": visual_window_id,
            "window_start_sequence": window_start_sequence,
            "target_sequence": target_sequence,
            "window_role": window_role,
            "provider_connection_isolated": provider_connection_isolated,
        },
    )
    inputs = {
        **common,
        **(
            {
                field: _safe_vlm_content_value(local_input_content[field])
                for field in _VLM_INPUT_FIELDS
                if local_input_content.get(field) not in (None, "", [], {})
                or isinstance(local_input_content.get(field), bool | int)
            }
            if local_input_content
            else {}
        ),
    }
    called = False
    result: _ResultT
    business_error: BaseException | None = None
    started_at = perf_counter()
    try:
        with trace(
            VISION_INFERENCE_OBSERVATION_NAME,
            run_type="llm",
            inputs=inputs,
            metadata={"model_role": "vlm"},
            tags=["vlm"],
        ) as generation:
            if isinstance(context, VisionInferenceTraceContext):
                link = VisionInferenceTraceLink(
                    trace_id=context.trace_id,
                    run_id=context.run_id,
                    span_id=str(generation.id),
                )
                context.last_link = link
                _notify_trace_link_fail_open(trace_link_callback, link)
            called = True
            try:
                result = call()
            except BaseException as exc:
                business_error = exc
                generation.end(error="VLM inference failed.")
            else:
                generation.end(
                    outputs=_vlm_output(
                        result,
                        fallback_latency_ms=max(
                            0,
                            int((perf_counter() - started_at) * 1000),
                        ),
                    )
                )
    except Exception:
        if business_error is not None:
            raise business_error
        if called:
            return result
        return call()
    if business_error is not None:
        raise business_error
    return result


def _notify_trace_link_fail_open(
    callback: Callable[[VisionInferenceTraceLink], None] | None,
    link: VisionInferenceTraceLink,
) -> None:
    if callback is None:
        return
    try:
        callback(link)
    except Exception:
        return


def _vision_inference_metadata(
    *,
    capability: str,
    source: str,
    media_kind: str,
    media_count: int,
    extra: Mapping[str, Any],
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "capability": capability,
        "source": source,
        "media_kind": media_kind,
        "media_count": max(0, int(media_count)),
        "prompt_version": VISION_INFERENCE_PROMPT_VERSION,
        "model_role": "vlm",
    }
    for key, value in extra.items():
        if value in (None, "", [], {}):
            continue
        if key in {"frame_sequence", "window_start_sequence", "target_sequence"}:
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                continue
        elif key in {"query_provided", "provider_connection_isolated"}:
            if not isinstance(value, bool):
                continue
        elif key == "window_role" and value not in {
            "target",
            "context",
            "background",
        }:
            continue
        elif _blocked_vlm_content_key(key.strip().lower()):
            continue
        metadata[key[:120]] = _safe_vlm_content_value(value)
    return metadata


def _visual_attachments(
    frame_refs: Sequence[str],
    frame_sequences: Sequence[int],
    *,
    include_frames: bool = True,
) -> dict[str, Attachment]:
    frames: list[bytes] = []
    attachments: dict[str, Attachment] = {}
    for frame_ref, sequence in zip(frame_refs, frame_sequences, strict=True):
        try:
            data = Path(frame_ref).read_bytes()
        except OSError:
            continue
        if not data or len(data) > _MAX_KEYFRAME_BYTES:
            continue
        frames.append(data)
        if include_frames:
            attachments[f"keyframe-{sequence:08d}"] = Attachment(
                mime_type="image/jpeg",
                data=data,
            )
    video = _selected_keyframe_mp4(frames)
    if video:
        attachments["selected-keyframes-video"] = Attachment(
            mime_type="video/mp4",
            data=video,
        )
    return attachments


def _selected_keyframe_mp4(frames: Sequence[bytes]) -> bytes | None:
    if not frames:
        return None
    try:
        completed = subprocess.run(
            (
                "/usr/bin/ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "image2pipe",
                "-framerate",
                "1",
                "-vcodec",
                "mjpeg",
                "-i",
                "pipe:0",
                "-an",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "frag_keyframe+empty_moov",
                "-f",
                "mp4",
                "pipe:1",
            ),
            input=b"".join(frames),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_KEYFRAME_VIDEO_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode or not completed.stdout:
        return None
    if len(completed.stdout) > _MAX_KEYFRAME_VIDEO_BYTES:
        return None
    return completed.stdout


def _visual_observation_output(result: object) -> dict[str, Any]:
    succeeded = bool(getattr(result, "succeeded", False))
    output = {"status": "succeeded" if succeeded else "failed"}
    error = getattr(result, "error", None)
    if isinstance(error, Mapping) and isinstance(error.get("code"), str):
        output["error_code"] = error["code"][:160]
    return output


def _vlm_output(result: object, *, fallback_latency_ms: int) -> dict[str, Any]:
    errors = getattr(result, "errors", None)
    output: dict[str, Any] = {
        "status": "failed" if isinstance(errors, list) and errors else "succeeded",
        "provider": _safe_text(getattr(result, "provider", None)),
        "model": _safe_text(getattr(result, "model", None)),
        "latency_ms": _result_latency_ms(result, fallback_latency_ms),
    }
    normalized = {
        key: value
        for key, value in _normalized_vlm_output(result).items()
        if key not in {"provider", "model", "latency_ms"}
    }
    if normalized:
        output["result"] = normalized
    error = _result_error(errors)
    if error is not None:
        output["error_code"] = error["code"]
    return output


def _normalized_vlm_output(result: object) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for field in _VLM_OUTPUT_FIELDS:
        value = getattr(result, field, None)
        if value not in (None, "", [], {}):
            payload[field] = _safe_vlm_content_value(value)
    return payload


def _safe_vlm_content_value(value: Any) -> Any:
    if isinstance(value, str):
        return value[:_MAX_VLM_CONTENT_TEXT_CHARS]
    if isinstance(value, bool | int | float) or value is None:
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, nested in list(value.items())[:_MAX_VLM_CONTENT_ITEMS]:
            normalized_key = str(key).strip().lower()
            if _blocked_vlm_content_key(normalized_key):
                continue
            result[str(key)[:120]] = _safe_vlm_content_value(nested)
        return result
    if isinstance(value, list | tuple):
        return [
            _safe_vlm_content_value(item)
            for item in value[:_MAX_VLM_CONTENT_ITEMS]
        ]
    return str(value)[:_MAX_VLM_CONTENT_TEXT_CHARS]


def _blocked_vlm_content_key(key: str) -> bool:
    return key in _BLOCKED_VLM_CONTENT_KEYS or key.endswith(
        ("_bytes", "_path", "_payload", "_ref", "_refs", "_uri")
    )


def _result_latency_ms(result: object, fallback: int) -> int:
    value = getattr(result, "latency_ms", None)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else fallback


def _result_error(errors: object) -> dict[str, str] | None:
    if not isinstance(errors, list) or not errors or not isinstance(errors[0], dict):
        return None
    first = errors[0]
    return {
        "code": _safe_text(first.get("code")) or "provider_call_failed",
        "message": "VLM inference failed.",
    }


def _safe_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
