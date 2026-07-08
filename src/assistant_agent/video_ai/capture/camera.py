"""Optional camera and iterable frame sources."""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from assistant_agent.video_ai.types import VideoFrame


class IterableFrameSource:
    """Wrap an iterable of frames as a video source."""

    def __init__(self, frames: Iterable[VideoFrame]) -> None:
        self.frames = frames

    def __iter__(self) -> Iterator[VideoFrame]:
        yield from self.frames


@dataclass(frozen=True)
class OpenCVCameraConfig:
    """Configuration for optional OpenCV camera capture."""

    source: int | str = 0
    read_fps: float = 5.0
    video_id: str = "camera"


class OpenCVCameraStream:
    """Capture frames from OpenCV when cv2 is installed by the operator."""

    def __init__(self, config: OpenCVCameraConfig | None = None) -> None:
        self.config = config or OpenCVCameraConfig()

    def __iter__(self) -> Iterator[VideoFrame]:
        try:
            import cv2
        except ImportError as exc:
            raise RuntimeError("OpenCVCameraStream requires cv2; install opencv-python in your local environment.") from exc

        capture = cv2.VideoCapture(self.config.source)
        if not capture.isOpened():
            raise RuntimeError(f"Unable to open video source: {self.config.source}")
        interval = 1.0 / max(self.config.read_fps, 0.001)
        frame_index = 0
        try:
            last_emit = 0.0
            while True:
                ok, frame = capture.read()
                if not ok:
                    break
                now = time.time()
                if now - last_emit < interval:
                    continue
                last_emit = now
                frame_index += 1
                yield VideoFrame(
                    frame_id=f"{self.config.video_id}_{frame_index:08d}",
                    timestamp_seconds=now,
                    pixels=_safe_pixels(frame),
                    width=int(getattr(frame, "shape", [0, 0])[1]),
                    height=int(getattr(frame, "shape", [0, 0])[0]),
                    metadata={"source": "opencv", "video_id": self.config.video_id},
                )
        finally:
            capture.release()


def _safe_pixels(frame: Any) -> Any:
    if hasattr(frame, "tolist"):
        return frame.tolist()
    return frame
