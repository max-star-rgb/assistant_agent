"""Video frame context storage for frame-based video understanding."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Protocol


REALTIME_VISUAL_TARGET_WINDOW_SIZE = 5
DEFAULT_VIDEO_CONTEXT_WINDOW_SIZE = REALTIME_VISUAL_TARGET_WINDOW_SIZE
REPO_ROOT = Path(__file__).resolve().parents[4]
DEMO_VIDEO_ROOT = REPO_ROOT / "demo_data" / "videos"
@dataclass(frozen=True)
class VideoFrame:
    """One frame received for a video stream."""

    video_id: str
    frame_id: str
    uri: str
    sequence: int
    timestamp_ms: int | None = None
    metadata: dict | None = None
    fingerprint: tuple[int, ...] | None = None
    fingerprint_width: int | None = None
    fingerprint_height: int | None = None


class VideoContextStore(Protocol):
    """Store recent frames per video id."""

    def append_frame(self, frame: VideoFrame) -> None:
        """Append a frame and keep only the configured recent window."""

    def get_recent_frames(self, video_id: str, *, limit: int | None = None) -> list[VideoFrame]:
        """Return a snapshot of the most recent frames for a video id."""

    def remove_video(self, video_id: str) -> list[VideoFrame]:
        """Remove and return all retained frames for one video id."""


class InMemoryVideoContextStore:
    """Small process-local sliding-window store for video frames."""

    def __init__(self, *, window_size: int = DEFAULT_VIDEO_CONTEXT_WINDOW_SIZE) -> None:
        self.window_size = window_size
        self._frames: dict[str, list[VideoFrame]] = {}
        self._lock = Lock()

    def append_frame(self, frame: VideoFrame) -> None:
        with self._lock:
            frames = [*self._frames.get(frame.video_id, []), frame]
            self._frames[frame.video_id] = frames[-self.window_size :]

    def get_recent_frames(self, video_id: str, *, limit: int | None = None) -> list[VideoFrame]:
        with self._lock:
            frames = list(self._frames.get(video_id, []))
        selected_limit = limit or self.window_size
        return frames[-selected_limit:]

    def frame_count(self, video_id: str) -> int:
        with self._lock:
            return len(self._frames.get(video_id, []))

    def remove_video(self, video_id: str) -> list[VideoFrame]:
        with self._lock:
            return self._frames.pop(video_id, [])


def load_demo_video_frames(
    store: VideoContextStore,
    video_id: str,
    *,
    root: Path = DEMO_VIDEO_ROOT,
) -> list[VideoFrame]:
    """Load local demo frames for a video id into a context store."""

    video_dir = root / video_id
    if not video_dir.exists() or not video_dir.is_dir():
        return []
    paths = sorted(path for path in video_dir.iterdir() if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"})
    frames = [
        VideoFrame(
            video_id=video_id,
            frame_id=path.stem,
            uri=str(path),
            sequence=index,
            timestamp_ms=(index - 1) * 1000,
            metadata={"source": "demo_data"},
        )
        for index, path in enumerate(paths, start=1)
    ]
    for frame in frames:
        store.append_frame(frame)
    return frames
