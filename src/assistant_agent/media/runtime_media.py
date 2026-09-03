"""Trusted media provenance projected from standard conversation messages."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from langchain_core.messages import BaseMessage, HumanMessage


@dataclass(frozen=True)
class RuntimeMediaSnapshot:
    """Media references split by trusted entry-projected source."""

    text: str = ""
    uploaded_image_ids: tuple[str, ...] = ()
    uploaded_video_ids: tuple[str, ...] = ()
    live_video_ids: tuple[str, ...] = ()
    visual_window_id: str | None = None
    visual_window_start_sequence: int | None = None
    visual_target_sequence: int | None = None

    @property
    def has_uploaded_media(self) -> bool:
        return bool(self.uploaded_image_ids or self.uploaded_video_ids)


def latest_runtime_media(state: Mapping[str, Any]) -> RuntimeMediaSnapshot:
    """Read explicitly sourced media from the latest real user message."""

    for message in reversed(tuple(state.get("messages", ()))):
        if not isinstance(message, HumanMessage):
            continue
        if isinstance(message.content, str):
            return RuntimeMediaSnapshot(text=message.content)
        texts: list[str] = []
        uploaded_image_ids: list[str] = []
        uploaded_video_ids: list[str] = []
        live_video_ids: list[str] = []
        visual_window_id: str | None = None
        visual_window_start_sequence: int | None = None
        visual_target_sequence: int | None = None
        for block in message.content:
            if not isinstance(block, Mapping):
                continue
            block_type = block.get("type")
            source = block.get("source")
            media_id = block.get("id")
            if block_type == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
            elif block_type == "video" and source == "live_camera" and media_id:
                live_video_ids.append(str(media_id))
                boundary = live_visual_window_boundary(block)
                if boundary is not None:
                    (
                        visual_window_id,
                        visual_window_start_sequence,
                        visual_target_sequence,
                    ) = boundary
            elif block_type in {"image", "image_url"}:
                media_ref = _uploaded_media_ref(block, media_kind="image")
                if media_ref is not None:
                    uploaded_image_ids.append(media_ref)
            elif block_type in {"video", "file"}:
                media_ref = _uploaded_video_ref(block)
                if media_ref is not None:
                    uploaded_video_ids.append(media_ref)
        return RuntimeMediaSnapshot(
            text="\n".join(texts),
            uploaded_image_ids=tuple(uploaded_image_ids),
            uploaded_video_ids=tuple(uploaded_video_ids),
            live_video_ids=tuple(live_video_ids),
            visual_window_id=visual_window_id,
            visual_window_start_sequence=visual_window_start_sequence,
            visual_target_sequence=visual_target_sequence,
        )
    return RuntimeMediaSnapshot()


def _uploaded_media_ref(
    block: Mapping[str, Any],
    *,
    media_kind: str,
) -> str | None:
    source = block.get("source")
    if source not in (None, "uploaded"):
        return None
    mime_type = block.get("mime_type")
    encoded = block.get("base64")
    if (
        isinstance(mime_type, str)
        and mime_type.startswith(f"{media_kind}/")
        and isinstance(encoded, str)
        and encoded
    ):
        return f"data:{mime_type};base64,{encoded}"
    url = block.get("url")
    if block.get("type") == "image_url" and isinstance(block.get("image_url"), Mapping):
        url = block["image_url"].get("url")
    if isinstance(url, str):
        if url.startswith(f"data:{media_kind}/"):
            return url
        parsed = urlsplit(url.strip())
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return url.strip()
    media_id = block.get("id")
    if source == "uploaded" and media_id:
        return str(media_id)
    return None


def _uploaded_video_ref(block: Mapping[str, Any]) -> str | None:
    if block.get("type") == "file":
        mime_type = block.get("mime_type")
        if isinstance(mime_type, str) and not mime_type.startswith("video/"):
            return None
        if mime_type is None and block.get("source") != "uploaded":
            return None
    return _uploaded_media_ref(block, media_kind="video")


def live_visual_window_boundary(
    block: Mapping[str, Any],
) -> tuple[str, int, int] | None:
    """Validate one trusted live-camera window boundary."""

    if (
        block.get("type") != "video"
        or block.get("source") != "live_camera"
        or not block.get("id")
    ):
        return None
    window_id = block.get("window_id")
    start_sequence = block.get("window_start_sequence")
    target_sequence = block.get("target_sequence")
    if not isinstance(window_id, str) or not 1 <= len(window_id) <= 160:
        return None
    if (
        isinstance(start_sequence, bool)
        or not isinstance(start_sequence, int)
        or start_sequence < 0
        or isinstance(target_sequence, bool)
        or not isinstance(target_sequence, int)
        or target_sequence < start_sequence
    ):
        return None
    return window_id, start_sequence, target_sequence


def without_uploaded_media_blocks(content: Any) -> Any:
    """Keep model-visible text while uploaded bytes remain available in state."""

    if not isinstance(content, (list, tuple)):
        return content
    return [
        block
        for block in content
        if not (isinstance(block, Mapping) and _is_uploaded_media_block(block))
    ]


def without_uploaded_media_messages(
    messages: list[BaseMessage] | tuple[BaseMessage, ...],
) -> list[BaseMessage]:
    """Remove uploaded payloads from a copy while preserving graph state."""

    return [
        message.model_copy(
            update={"content": without_uploaded_media_blocks(message.content)}
        )
        if isinstance(message, HumanMessage)
        else message
        for message in messages
    ]


def _is_uploaded_media_block(block: Mapping[str, Any]) -> bool:
    if block.get("source") not in (None, "uploaded"):
        return False
    block_type = block.get("type")
    if block_type in {"image", "image_url"}:
        return True
    if block_type == "video":
        return block.get("source") != "live_camera"
    if block_type == "file":
        mime_type = block.get("mime_type")
        return block.get("source") == "uploaded" or (
            isinstance(mime_type, str) and mime_type.startswith("video/")
        )
    return False


__all__ = [
    "RuntimeMediaSnapshot",
    "latest_runtime_media",
    "live_visual_window_boundary",
    "without_uploaded_media_blocks",
    "without_uploaded_media_messages",
]
