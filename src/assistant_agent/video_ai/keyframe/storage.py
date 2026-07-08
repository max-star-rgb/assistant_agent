"""Local keyframe persistence helpers."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Protocol

from assistant_agent.video_ai.detection.frame_difference import grayscale_fingerprint
from assistant_agent.video_ai.types import VideoFrame


class KeyframeStorage(Protocol):
    """Persist or normalize selected keyframes before model calls."""

    def store(self, frame: VideoFrame) -> VideoFrame:
        """Return a frame with a usable prompt-safe keyframe URI."""


class NoopKeyframeStorage:
    """Keep the incoming frame reference unchanged."""

    def store(self, frame: VideoFrame) -> VideoFrame:
        return frame


class FileKeyframeStorage:
    """Persist frames without a usable URI as local grayscale thumbnails."""

    def __init__(self, root: Path | str = ".local/video_ai/keyframes") -> None:
        self.root = Path(root)

    def store(self, frame: VideoFrame) -> VideoFrame:
        if _usable_uri(frame.uri):
            return frame
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{_safe_name(frame.frame_id)}.pgm"
        values = grayscale_fingerprint(frame, (160, 90))
        if not values:
            return frame
        rows = []
        width = 160
        for offset in range(0, len(values), width):
            row = " ".join(str(int(max(0.0, min(1.0, value)) * 255)) for value in values[offset : offset + width])
            rows.append(row)
        path.write_text(f"P2\n160 90\n255\n{chr(10).join(rows)}\n", encoding="ascii")
        metadata = {**frame.metadata, "keyframe_path": str(path)}
        return replace(frame, uri=str(path), metadata=metadata)


def _usable_uri(uri: str | None) -> bool:
    if not uri:
        return False
    if uri.startswith(("memory://", "mock://")):
        return False
    return True


def _safe_name(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in value)
    return cleaned or "frame"
