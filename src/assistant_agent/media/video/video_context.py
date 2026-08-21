"""Video frame context storage for frame-based video understanding."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from threading import Lock
from typing import Protocol


REALTIME_VISUAL_TARGET_WINDOW_SIZE = 8
DEFAULT_VIDEO_CONTEXT_WINDOW_SIZE = REALTIME_VISUAL_TARGET_WINDOW_SIZE
REPO_ROOT = Path(__file__).resolve().parents[4]
DEMO_VIDEO_ROOT = REPO_ROOT / "demo_data" / "videos"
DEFAULT_AGENT_SERVER_VIDEO_CONTEXT_PATH = (
    REPO_ROOT / ".data" / "agent_server_video_context.sqlite3"
)


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


class SQLiteVideoContextStore:
    """Small durable frame index shared by media ingress and graph workers.

    JPEG payloads stay in the bounded media directory.  This store persists
    only stable references and prompt-safe metadata, so Graph State never owns
    decoder objects or raw H.264 payloads.
    """

    def __init__(
        self,
        path: Path | str = DEFAULT_AGENT_SERVER_VIDEO_CONTEXT_PATH,
        *,
        window_size: int = DEFAULT_VIDEO_CONTEXT_WINDOW_SIZE,
    ) -> None:
        if window_size <= 0:
            raise ValueError("window_size must be positive")
        self.path = Path(path)
        self.window_size = window_size
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def append_frame(self, frame: VideoFrame) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR REPLACE INTO video_frames (
                    video_id, frame_id, uri, sequence, timestamp_ms,
                    metadata_json, fingerprint_json, fingerprint_width,
                    fingerprint_height
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    frame.video_id,
                    frame.frame_id,
                    frame.uri,
                    frame.sequence,
                    frame.timestamp_ms,
                    json.dumps(frame.metadata) if frame.metadata is not None else None,
                    json.dumps(frame.fingerprint) if frame.fingerprint is not None else None,
                    frame.fingerprint_width,
                    frame.fingerprint_height,
                ),
            )
            connection.execute(
                """
                DELETE FROM video_frames
                WHERE video_id = ? AND sequence NOT IN (
                    SELECT sequence FROM video_frames
                    WHERE video_id = ?
                    ORDER BY sequence DESC
                    LIMIT ?
                )
                """,
                (frame.video_id, frame.video_id, self.window_size),
            )

    def get_recent_frames(
        self,
        video_id: str,
        *,
        limit: int | None = None,
    ) -> list[VideoFrame]:
        selected_limit = limit or self.window_size
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT video_id, frame_id, uri, sequence, timestamp_ms,
                       metadata_json, fingerprint_json, fingerprint_width,
                       fingerprint_height
                FROM video_frames
                WHERE video_id = ?
                ORDER BY sequence DESC
                LIMIT ?
                """,
                (video_id, selected_limit),
            ).fetchall()
        return [_frame_from_row(row) for row in reversed(rows)]

    def remove_video(self, video_id: str) -> list[VideoFrame]:
        frames = self.get_recent_frames(video_id, limit=self.window_size)
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM video_frames WHERE video_id = ?",
                (video_id,),
            )
        return frames

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS video_frames (
                    video_id TEXT NOT NULL,
                    frame_id TEXT NOT NULL,
                    uri TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    timestamp_ms INTEGER,
                    metadata_json TEXT,
                    fingerprint_json TEXT,
                    fingerprint_width INTEGER,
                    fingerprint_height INTEGER,
                    PRIMARY KEY (video_id, sequence)
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5.0)


def _frame_from_row(row: tuple) -> VideoFrame:
    fingerprint = json.loads(row[6]) if row[6] is not None else None
    return VideoFrame(
        video_id=row[0],
        frame_id=row[1],
        uri=row[2],
        sequence=row[3],
        timestamp_ms=row[4],
        metadata=json.loads(row[5]) if row[5] is not None else None,
        fingerprint=tuple(fingerprint) if fingerprint is not None else None,
        fingerprint_width=row[7],
        fingerprint_height=row[8],
    )


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
