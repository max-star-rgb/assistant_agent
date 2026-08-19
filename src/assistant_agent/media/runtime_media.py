"""Trusted media provenance projected from standard conversation messages."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import HumanMessage


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
            elif (
                block_type in {"image", "image_url"}
                and source == "uploaded"
                and media_id
            ):
                uploaded_image_ids.append(str(media_id))
            elif (
                block_type in {"video", "file"}
                and source == "uploaded"
                and media_id
            ):
                uploaded_video_ids.append(str(media_id))
            elif block_type == "video" and source == "live_camera" and media_id:
                live_video_ids.append(str(media_id))
                boundary = live_visual_window_boundary(block)
                if boundary is not None:
                    (
                        visual_window_id,
                        visual_window_start_sequence,
                        visual_target_sequence,
                    ) = boundary
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


__all__ = [
    "RuntimeMediaSnapshot",
    "latest_runtime_media",
    "live_visual_window_boundary",
]
