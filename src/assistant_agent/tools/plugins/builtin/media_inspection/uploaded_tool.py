"""Native function Tool for user-uploaded image and explicit-video analysis."""

from __future__ import annotations

import base64
import binascii
import json
import math
import subprocess
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Any
from urllib.parse import urlsplit

from langchain_core.tools import BaseTool, ToolException, tool
from langgraph.prebuilt import ToolRuntime
from pydantic import Field

from assistant_agent.media.runtime_media import latest_runtime_media
from assistant_agent.media.vision.models import (
    VisionUnderstandingRequest,
)
from assistant_agent.media.vision.observability import (
    invoke_native_vision_model,
    observe_vision_inference,
)
from assistant_agent.media.vision.vision_client import VisionUnderstandingClient
from assistant_agent.native_agent.context import (
    AssistantRunContext,
    authenticated_user_identity,
)
from assistant_agent.providers.provider_errors import (
    ProviderAdapterError,
    build_provider_error,
)
from assistant_agent.tools.availability import ToolAvailability
from assistant_agent.tools.ids import (
    IMAGE_UNDERSTANDING_CAPABILITY,
    UPLOADED_MEDIA_INSPECT_TOOL_NAME,
)
from assistant_agent.tools.native_boundary import (
    configure_builtin_tool,
    native_content_and_artifact,
    native_tool_exception,
)


def create_uploaded_media_inspect_tool(
    client: VisionUnderstandingClient,
    *,
    max_video_bytes: int = 52_428_800,
    max_video_seconds: float = 60.0,
) -> BaseTool:
    """Create the native Tool while retaining one process-owned VLM client."""

    @tool(
        UPLOADED_MEDIA_INSPECT_TOOL_NAME,
        response_format="content_and_artifact",
    )
    def uploaded_media_inspect(
        question: Annotated[
            str,
            Field(
                min_length=1,
                max_length=500,
                description="需要从当前上传图片或视频中重点回答的问题。",
            ),
        ],
        runtime: ToolRuntime[AssistantRunContext],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """分析当前请求中由用户主动上传的图片或视频附件。"""

        def inspect_uploaded_media() -> tuple[list[dict[str, Any]], dict[str, Any]]:
            state = runtime.state if isinstance(runtime.state, dict) else {}
            media = latest_runtime_media(state)
            if not media.has_uploaded_media:
                raise ToolException(
                    "uploaded_media_required: 当前请求没有用户主动上传的图片或视频"
                )
            if media.uploaded_image_ids and media.uploaded_video_ids:
                raise ToolException(
                    "uploaded_media_mixed_unsupported: 请每次只上传图片或一个 MP4 视频"
                )
            if len(media.uploaded_video_ids) > 1:
                raise ToolException(
                    "uploaded_video_count_unsupported: 请每次只上传一个 MP4 视频"
                )
            if len(media.uploaded_image_ids) > 5:
                raise ToolException(
                    "uploaded_image_count_unsupported: 最多上传 5 张图片"
                )
            if media.uploaded_video_ids:
                with _sample_uploaded_video(
                    media.uploaded_video_ids[0],
                    cwd=runtime.context.cwd,
                    max_bytes=max_video_bytes,
                    max_seconds=max_video_seconds,
                ) as frame_refs:
                    request = _vision_request(
                        image_refs=list(frame_refs),
                        question=question,
                        media_text=media.text,
                        runtime=runtime,
                        memory_context=state.get("memory_context", ()),
                    )
                    result = _understand(client, request, media_kind="explicit_video")
                data = result.model_copy(
                    update={"source": "uploaded", "media_kind": "explicit_video"}
                ).model_dump(mode="json")
            else:
                with _materialized_uploaded_images(
                    media.uploaded_image_ids,
                    cwd=runtime.context.cwd,
                    max_bytes=max_video_bytes,
                ) as image_refs:
                    request = _vision_request(
                        image_refs=list(image_refs),
                        question=question,
                        media_text=media.text,
                        runtime=runtime,
                        memory_context=state.get("memory_context", ()),
                    )
                    result = _understand(client, request, media_kind="image")
                data = result.model_dump(mode="json")
            data.pop("media_refs", None)
            return native_content_and_artifact(_vision_model_observation(data), data)

        try:
            return inspect_uploaded_media()
        except ToolException:
            raise
        except Exception as exc:
            raise native_tool_exception(
                exc, tool_name=UPLOADED_MEDIA_INSPECT_TOOL_NAME
            ) from exc

    return configure_builtin_tool(
        uploaded_media_inspect,
        availability=ToolAvailability.UPLOADED_MEDIA_PRESENT.value,
        bounded_expected_errors=True,
    )


def _vision_request(
    *,
    image_refs: list[str],
    question: str,
    media_text: str,
    runtime: ToolRuntime[AssistantRunContext],
    memory_context: Any,
) -> VisionUnderstandingRequest:
    return VisionUnderstandingRequest(
        image_ids=image_refs,
        question=question,
        user_query=media_text,
        user_id=authenticated_user_identity(runtime),
        session_id=getattr(runtime.execution_info, "thread_id", None),
        metadata={"media_source": "uploaded"},
        memory_context=list(memory_context) or None,
    )


def _understand(
    client: VisionUnderstandingClient,
    request: VisionUnderstandingRequest,
    *,
    media_kind: str,
):
    try:
        if getattr(client, "traces_as_chat_model", False):
            return invoke_native_vision_model(
                lambda config: client.understand(request, config=config),
                context=None,
                capability=IMAGE_UNDERSTANDING_CAPABILITY,
                source="request_image",
                media_kind=media_kind,
                media_count=len(request.image_ids),
                query_provided=bool(request.question or request.user_query),
            )
        return observe_vision_inference(
            lambda: client.understand(request),
            context=None,
            capability=IMAGE_UNDERSTANDING_CAPABILITY,
            source="request_image",
            media_kind=media_kind,
            media_count=len(request.image_ids),
        )
    except ProviderAdapterError as exc:
        adapter_config = getattr(getattr(client, "image_adapter", None), "config", None)
        error = build_provider_error(
            exc.code,
            exc.message,
            provider=getattr(adapter_config, "provider", "unknown"),
            capability=IMAGE_UNDERSTANDING_CAPABILITY,
        )
        raise ToolException(f"{error.code}: {error.message}") from exc
    except ValueError as exc:
        raise native_tool_exception(
            exc, tool_name=UPLOADED_MEDIA_INSPECT_TOOL_NAME
        ) from exc


@contextmanager
def _materialized_uploaded_images(
    image_refs: tuple[str, ...],
    *,
    cwd: Path,
    max_bytes: int,
):
    with TemporaryDirectory(prefix="assistant-uploaded-image-") as temporary:
        validated: list[str] = []
        total_bytes = 0
        root = cwd.resolve()
        for index, image_ref in enumerate(image_refs):
            if image_ref.startswith("data:image/"):
                header, separator, encoded = image_ref.partition(",")
                mime_type = header.removeprefix("data:").partition(";")[0]
                suffixes = {
                    "image/jpeg": ".jpg",
                    "image/png": ".png",
                    "image/webp": ".webp",
                }
                if (
                    not separator
                    or ";base64" not in header
                    or mime_type not in suffixes
                ):
                    raise ToolException("uploaded_image_invalid: 图片 data URL 无效")
                if len(encoded) > ((max_bytes + 2) // 3) * 4:
                    raise ToolException("uploaded_image_size_invalid: 图片超过大小限制")
                try:
                    data = base64.b64decode(encoded, validate=True)
                except (binascii.Error, ValueError) as exc:
                    raise ToolException(
                        "uploaded_image_invalid: 图片 Base64 无效"
                    ) from exc
                if not data or len(data) > max_bytes:
                    raise ToolException(
                        f"uploaded_image_size_invalid: 图片大小必须在 1 到 {max_bytes} 字节之间"
                    )
                total_bytes += len(data)
                if total_bytes > max_bytes:
                    raise ToolException(
                        "uploaded_image_size_invalid: 图片总大小超过限制"
                    )
                path = Path(temporary) / f"image-{index}{suffixes[mime_type]}"
                path.write_bytes(data)
                validated.append(str(path))
                continue
            parsed = urlsplit(image_ref)
            if parsed.scheme in {"http", "https"} and parsed.netloc:
                validated.append(image_ref)
                continue
            path = Path(image_ref)
            path = (root / path).resolve() if not path.is_absolute() else path.resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise ToolException(
                    "uploaded_image_ref_invalid: 图片文件必须位于当前工作目录内"
                )
            size = path.stat().st_size
            if size <= 0 or size > max_bytes:
                raise ToolException(
                    f"uploaded_image_size_invalid: 图片大小必须在 1 到 {max_bytes} 字节之间"
                )
            total_bytes += size
            if total_bytes > max_bytes:
                raise ToolException("uploaded_image_size_invalid: 图片总大小超过限制")
            validated.append(str(path))
        yield tuple(validated)


@contextmanager
def _sample_uploaded_video(
    video_ref: str,
    *,
    cwd: Path,
    max_bytes: int,
    max_seconds: float,
):
    with TemporaryDirectory(prefix="assistant-uploaded-video-") as temporary:
        root = Path(temporary)
        video_path = _materialize_uploaded_video(
            video_ref,
            cwd=cwd,
            destination=root / "input.mp4",
            max_bytes=max_bytes,
        )
        duration = _video_duration(video_path)
        if not math.isfinite(duration) or duration <= 0 or duration > max_seconds:
            raise ToolException(
                f"uploaded_video_duration_invalid: 视频时长必须大于 0 且不超过 {max_seconds:g} 秒"
            )
        output_pattern = root / "frame-%02d.jpg"
        completed = subprocess.run(
            [
                "/usr/bin/ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-f",
                "mov",
                "-enable_drefs",
                "0",
                "-use_absolute_path",
                "0",
                "-i",
                str(video_path),
                "-vf",
                f"fps={5 / duration:.8f},scale='min(1024,iw)':-2",
                "-frames:v",
                "5",
                "-q:v",
                "3",
                "-y",
                str(output_pattern),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=max(10.0, max_seconds),
            check=False,
        )
        frames = tuple(sorted(root.glob("frame-*.jpg")))
        if completed.returncode or not frames:
            raise ToolException("uploaded_video_decode_failed: 无法从上传视频提取画面")
        yield tuple(str(frame) for frame in frames)


def _materialize_uploaded_video(
    video_ref: str,
    *,
    cwd: Path,
    destination: Path,
    max_bytes: int,
) -> Path:
    if video_ref.startswith("data:video/"):
        header, separator, encoded = video_ref.partition(",")
        if not separator or header != "data:video/mp4;base64":
            raise ToolException("uploaded_video_invalid: 视频 data URL 无效")
        if len(encoded) > ((max_bytes + 2) // 3) * 4:
            raise ToolException("uploaded_video_size_invalid: 视频超过大小限制")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ToolException("uploaded_video_invalid: 视频 Base64 无效") from exc
        if not data or len(data) > max_bytes:
            raise ToolException(
                f"uploaded_video_size_invalid: 视频大小必须在 1 到 {max_bytes} 字节之间"
            )
        destination.write_bytes(data)
        return destination

    root = cwd.resolve()
    path = Path(video_ref)
    path = (root / path).resolve() if not path.is_absolute() else path.resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ToolException(
            "uploaded_video_ref_invalid: 视频文件必须位于当前工作目录内"
        )
    if path.suffix.lower() != ".mp4":
        raise ToolException("uploaded_video_format_invalid: 仅支持 MP4 视频")
    size = path.stat().st_size
    if size <= 0 or size > max_bytes:
        raise ToolException(
            f"uploaded_video_size_invalid: 视频大小必须在 1 到 {max_bytes} 字节之间"
        )
    return path


def _video_duration(path: Path) -> float:
    completed = subprocess.run(
        [
            "/usr/bin/ffprobe",
            "-v",
            "error",
            "-f",
            "mov",
            "-show_entries",
            "format=duration,format_name:stream=codec_type,width,height",
            "-of",
            "json",
            str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10.0,
        check=False,
    )
    if completed.returncode:
        raise ToolException("uploaded_video_probe_failed: 无法读取上传视频")
    try:
        payload = json.loads(completed.stdout)
        format_info = payload["format"]
        if "mp4" not in str(format_info["format_name"]).split(","):
            raise ToolException("uploaded_video_format_invalid: 仅支持 MP4 视频")
        video_streams = [
            stream
            for stream in payload.get("streams", ())
            if stream.get("codec_type") == "video"
        ]
        if not video_streams:
            raise ToolException("uploaded_video_stream_invalid: MP4 中没有视频轨道")
        for stream in video_streams:
            width = int(stream.get("width", 0))
            height = int(stream.get("height", 0))
            if (
                width <= 0
                or height <= 0
                or width > 8192
                or height > 8192
                or width * height > 16_777_216
            ):
                raise ToolException(
                    "uploaded_video_dimensions_invalid: 视频分辨率超出限制"
                )
        return float(format_info["duration"])
    except ToolException:
        raise
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ToolException(
            "uploaded_video_probe_failed: 无法读取上传视频时长"
        ) from exc


def _vision_model_observation(data: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "summary",
        "objects",
        "people",
        "actions",
        "events",
        "colors",
        "materials",
        "scene",
        "products",
        "brands",
        "style_tags",
        "text_in_media",
        "text_in_video",
        "confidence",
        "source",
        "media_kind",
        "media_refs",
        "errors",
    )
    return {key: data[key] for key in keys if data.get(key) not in (None, "", [], {})}


__all__ = ["create_uploaded_media_inspect_tool"]
