"""Bounded H.264 segment archiving for the external visual Memory Service."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import logging
from pathlib import Path
import secrets
import sqlite3
import subprocess
from threading import Lock
import time

from assistant_agent.memory.remote_service import (
    MemoryMediaFile,
    RemoteMemoryServiceClient,
)


logger = logging.getLogger(__name__)

Muxer = Callable[[Path, Path, float], None]


@dataclass(frozen=True)
class ArchivedVideoSegment:
    file_id: str
    path: Path
    start_time: str


@dataclass(frozen=True)
class PublishedMedia:
    token: str
    url: str


@dataclass(frozen=True)
class VideoArchiveManifestEntry:
    file_id: str
    path: Path
    start_time: str
    user_id: str
    session_id: str
    status: str


class VideoArchiveManifest:
    """Small durable ledger for MP4 segments awaiting remote ingestion."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS video_archive_segments (
                    file_id TEXT PRIMARY KEY,
                    path TEXT NOT NULL,
                    start_time TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    status TEXT NOT NULL
                )
                """
            )

    def save(
        self,
        *,
        file_id: str,
        path: Path | str,
        start_time: str,
        user_id: str,
        session_id: str,
        status: str,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO video_archive_segments (
                    file_id, path, start_time, user_id, session_id, status
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(file_id) DO UPDATE SET
                    path=excluded.path,
                    start_time=excluded.start_time,
                    user_id=excluded.user_id,
                    session_id=excluded.session_id,
                    status=excluded.status
                """,
                (
                    file_id,
                    str(Path(path).resolve()),
                    start_time,
                    user_id,
                    session_id,
                    status,
                ),
            )

    def remove(self, file_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM video_archive_segments WHERE file_id = ?",
                (file_id,),
            )

    def pending(self) -> tuple[VideoArchiveManifestEntry, ...]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT file_id, path, start_time, user_id, session_id, status
                FROM video_archive_segments
                ORDER BY rowid
                """
            ).fetchall()
        return tuple(
            VideoArchiveManifestEntry(
                file_id=str(row[0]),
                path=Path(str(row[1])),
                start_time=str(row[2]),
                user_id=str(row[3]),
                session_id=str(row[4]),
                status=str(row[5]),
            )
            for row in rows
            if Path(str(row[1])).is_file()
        )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5.0)


@dataclass
class _OpenSegment:
    sequence: int
    raw_path: Path
    started_at_seconds: float
    start_time: str
    frame_rate: float


class H264ArchiveRecorder:
    """Append independent Annex-B frames and atomically publish MP4 segments."""

    def __init__(
        self,
        *,
        root: Path | str,
        segment_seconds: float = 30.0,
        muxer: Muxer | None = None,
        ffmpeg_binary: str = "/usr/bin/ffmpeg",
    ) -> None:
        if segment_seconds <= 0:
            raise ValueError("segment_seconds must be positive")
        self.root = Path(root)
        self.segment_seconds = segment_seconds
        self._muxer = muxer or (
            lambda source, output, fps: _remux_h264_to_mp4(
                source,
                output,
                fps,
                ffmpeg_binary=ffmpeg_binary,
            )
        )
        self._open: dict[str, _OpenSegment] = {}
        self._next_sequence: dict[str, int] = {}
        self._lock = Lock()

    def append(
        self,
        *,
        session_id: str,
        h264_bytes: bytes,
        captured_at: str | None,
        frame_rate: float,
    ) -> tuple[ArchivedVideoSegment, ...]:
        if not session_id.strip():
            raise ValueError("archive session_id is required")
        if not h264_bytes:
            raise ValueError("archive frame is empty")
        timestamp, normalized_time = _capture_time(captured_at)
        completed: list[ArchivedVideoSegment] = []
        with self._lock:
            current = self._open.get(session_id)
            if (
                current is not None
                and timestamp - current.started_at_seconds >= self.segment_seconds
            ):
                completed.append(self._finalize(session_id, current))
                current = None
            if current is None:
                current = self._start_segment(
                    session_id,
                    timestamp=timestamp,
                    start_time=normalized_time,
                    frame_rate=frame_rate,
                )
            with current.raw_path.open("ab") as handle:
                handle.write(h264_bytes)
        return tuple(completed)

    def flush(self, session_id: str) -> ArchivedVideoSegment | None:
        with self._lock:
            current = self._open.get(session_id)
            if current is None:
                return None
            return self._finalize(session_id, current)

    def segment_for_existing_file(
        self,
        *,
        path: Path | str,
        file_id: str,
        start_time: str,
    ) -> ArchivedVideoSegment:
        return ArchivedVideoSegment(
            file_id=file_id,
            path=Path(path).resolve(),
            start_time=start_time,
        )

    def _start_segment(
        self,
        session_id: str,
        *,
        timestamp: float,
        start_time: str,
        frame_rate: float,
    ) -> _OpenSegment:
        sequence = self._next_sequence.get(session_id, 0) + 1
        self._next_sequence[session_id] = sequence
        session_digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]
        directory = self.root / session_digest
        directory.mkdir(parents=True, exist_ok=True)
        raw_path = directory / f"segment-{sequence:06d}.h264.part"
        current = _OpenSegment(
            sequence=sequence,
            raw_path=raw_path,
            started_at_seconds=timestamp,
            start_time=start_time,
            frame_rate=frame_rate if frame_rate > 0 else 25.0,
        )
        self._open[session_id] = current
        return current

    def _finalize(
        self,
        session_id: str,
        current: _OpenSegment,
    ) -> ArchivedVideoSegment:
        self._open.pop(session_id, None)
        digest = hashlib.sha256(
            f"{session_id}:{current.sequence}:{current.start_time}".encode("utf-8")
        ).hexdigest()[:24]
        file_id = f"video-{digest}"
        output_path = current.raw_path.with_name(f"{file_id}.mp4")
        partial_path = output_path.with_suffix(".mp4.part")
        try:
            self._muxer(current.raw_path, partial_path, current.frame_rate)
            if not partial_path.is_file() or partial_path.stat().st_size == 0:
                raise RuntimeError("H264 muxer did not produce an MP4")
            partial_path.replace(output_path)
        finally:
            current.raw_path.unlink(missing_ok=True)
            partial_path.unlink(missing_ok=True)
        return ArchivedVideoSegment(
            file_id=file_id,
            path=output_path.resolve(),
            start_time=current.start_time,
        )


@dataclass(frozen=True)
class _DownloadGrant:
    path: Path
    expires_at: float


class MediaDownloadRegistry:
    """In-process, task-scoped capability URLs for completed MP4 segments."""

    def __init__(
        self,
        *,
        base_url: str,
        ttl_seconds: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._grants: dict[str, _DownloadGrant] = {}
        self._lock = Lock()

    def publish(self, path: Path | str, *, file_id: str) -> PublishedMedia:
        resolved = Path(path).resolve()
        if not resolved.is_file():
            raise FileNotFoundError(resolved)
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._grants[token] = _DownloadGrant(
                path=resolved,
                expires_at=self._clock() + self.ttl_seconds,
            )
        return PublishedMedia(
            token=token,
            url=f"{self.base_url}/internal/memory-media/{token}",
        )

    def resolve(self, token: str) -> Path | None:
        with self._lock:
            grant = self._grants.get(token)
            if grant is None:
                return None
            if grant.expires_at < self._clock() or not grant.path.is_file():
                self._grants.pop(token, None)
                return None
            return grant.path

    def revoke(self, token: str) -> None:
        with self._lock:
            self._grants.pop(token, None)


class RemoteVideoArchiveUploader:
    """Submit one published segment and clean it only after remote completion."""

    def __init__(
        self,
        *,
        client: RemoteMemoryServiceClient,
        registry: MediaDownloadRegistry,
        poll_interval_seconds: float,
        max_status_polls: int = 1_800,
        manifest: VideoArchiveManifest | None = None,
    ) -> None:
        self.client = client
        self.registry = registry
        self.poll_interval_seconds = poll_interval_seconds
        self.max_status_polls = max_status_polls
        self.manifest = manifest

    async def process(
        self,
        segment: ArchivedVideoSegment,
        *,
        user_id: str,
        session_id: str,
    ) -> str:
        self._save(segment, user_id=user_id, session_id=session_id, status="ready")
        published = self.registry.publish(segment.path, file_id=segment.file_id)
        try:
            self._save(
                segment,
                user_id=user_id,
                session_id=session_id,
                status="submitting",
            )
            result = await self.client.upload_media(
                user_id=user_id,
                session_id=session_id,
                files=(
                    MemoryMediaFile(
                        file_id=segment.file_id,
                        file_url=published.url,
                        filename=segment.path.name,
                        media_type="video",
                        start_time=segment.start_time,
                        metadata={},
                    ),
                ),
            )
            if not result.task_id or result.accepted_count < 1:
                self._save(
                    segment,
                    user_id=user_id,
                    session_id=session_id,
                    status="failed",
                )
                return "failed"
            self._save(
                segment,
                user_id=user_id,
                session_id=session_id,
                status="processing",
            )
            for _attempt in range(self.max_status_polls):
                status = await self.client.task_status(
                    user_id=user_id,
                    task_id=result.task_id,
                )
                if status.status == "completed":
                    segment.path.unlink(missing_ok=True)
                    if self.manifest is not None:
                        self.manifest.remove(segment.file_id)
                    return "completed"
                if status.status == "failed":
                    self._save(
                        segment,
                        user_id=user_id,
                        session_id=session_id,
                        status="failed",
                    )
                    return "failed"
                await asyncio.sleep(self.poll_interval_seconds)
            self._save(
                segment,
                user_id=user_id,
                session_id=session_id,
                status="timeout",
            )
            return "timeout"
        except Exception as exc:  # noqa: BLE001 - background dependency boundary.
            logger.warning(
                "remote_visual_memory_upload_failed file=%s error_type=%s",
                segment.file_id,
                type(exc).__name__,
            )
            return "failed"
        finally:
            self.registry.revoke(published.token)

    def _save(
        self,
        segment: ArchivedVideoSegment,
        *,
        user_id: str,
        session_id: str,
        status: str,
    ) -> None:
        if self.manifest is not None:
            self.manifest.save(
                file_id=segment.file_id,
                path=segment.path,
                start_time=segment.start_time,
                user_id=user_id,
                session_id=session_id,
                status=status,
            )


@dataclass
class _ArchiveConnection:
    user_id: str
    session_id: str
    tail: asyncio.Task[None] | None = None
    pending_frames: int = 0


class RemoteVideoArchiveService:
    """Preserve per-connection frame order without blocking the WebSocket loop."""

    def __init__(
        self,
        *,
        recorder: H264ArchiveRecorder,
        uploader: RemoteVideoArchiveUploader,
        max_pending_frames: int = 512,
    ) -> None:
        self.recorder = recorder
        self.uploader = uploader
        self.max_pending_frames = max_pending_frames
        self._connections: dict[str, _ArchiveConnection] = {}
        self._upload_tasks: set[asyncio.Task[str]] = set()

    def open_session(
        self,
        *,
        connection_id: str,
        user_id: str,
        session_id: str,
    ) -> None:
        if connection_id in self._connections:
            raise ValueError("archive connection is already open")
        self._connections[connection_id] = _ArchiveConnection(
            user_id=user_id,
            session_id=session_id,
        )

    def enqueue_frame(
        self,
        *,
        connection_id: str,
        h264_bytes: bytes,
        captured_at: str | None,
        frame_rate: float,
    ) -> bool:
        connection = self._connections.get(connection_id)
        if connection is None or connection.pending_frames >= self.max_pending_frames:
            return False
        previous = connection.tail
        connection.pending_frames += 1
        connection.tail = asyncio.create_task(
            self._append_after(
                previous,
                connection=connection,
                h264_bytes=h264_bytes,
                captured_at=captured_at,
                frame_rate=frame_rate,
            ),
            name="remote-visual-memory-archive-frame",
        )
        return True

    async def _append_after(
        self,
        previous: asyncio.Task[None] | None,
        *,
        connection: _ArchiveConnection,
        h264_bytes: bytes,
        captured_at: str | None,
        frame_rate: float,
    ) -> None:
        try:
            if previous is not None:
                await asyncio.gather(previous, return_exceptions=True)
            segments = await asyncio.to_thread(
                self.recorder.append,
                session_id=connection.session_id,
                h264_bytes=h264_bytes,
                captured_at=captured_at,
                frame_rate=frame_rate,
            )
            for segment in segments:
                self._schedule_upload(segment, connection=connection)
        except Exception as exc:  # noqa: BLE001 - optional archive lane.
            logger.warning(
                "remote_visual_memory_archive_failed error_type=%s",
                type(exc).__name__,
            )
        finally:
            connection.pending_frames -= 1

    async def close_session(self, connection_id: str) -> None:
        connection = self._connections.pop(connection_id, None)
        if connection is None:
            return
        if connection.tail is not None:
            await asyncio.gather(connection.tail, return_exceptions=True)
        segment = await asyncio.to_thread(
            self.recorder.flush,
            connection.session_id,
        )
        if segment is not None:
            self._schedule_upload(segment, connection=connection)

    def _schedule_upload(
        self,
        segment: ArchivedVideoSegment,
        *,
        connection: _ArchiveConnection,
    ) -> None:
        task = asyncio.create_task(
            self.uploader.process(
                segment,
                user_id=connection.user_id,
                session_id=connection.session_id,
            ),
            name=f"remote-visual-memory-upload:{segment.file_id}",
        )
        self._upload_tasks.add(task)
        task.add_done_callback(self._upload_tasks.discard)

    async def wait_for_uploads(self) -> None:
        if self._upload_tasks:
            await asyncio.gather(*tuple(self._upload_tasks), return_exceptions=True)

    async def recover(self) -> None:
        manifest = getattr(self.uploader, "manifest", None)
        if manifest is None:
            return
        for entry in manifest.pending():
            self._schedule_upload(
                ArchivedVideoSegment(
                    file_id=entry.file_id,
                    path=entry.path,
                    start_time=entry.start_time,
                ),
                connection=_ArchiveConnection(
                    user_id=entry.user_id,
                    session_id=entry.session_id,
                ),
            )

    async def aclose(self) -> None:
        for connection_id in tuple(self._connections):
            await self.close_session(connection_id)
        for task in tuple(self._upload_tasks):
            task.cancel()
        if self._upload_tasks:
            await asyncio.gather(*tuple(self._upload_tasks), return_exceptions=True)


def _capture_time(value: str | None) -> tuple[float, str]:
    if value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp(), parsed.isoformat()
        except ValueError:
            pass
    now = datetime.now(timezone.utc)
    return now.timestamp(), now.isoformat()


def _remux_h264_to_mp4(
    source: Path,
    output: Path,
    frame_rate: float,
    *,
    ffmpeg_binary: str,
) -> None:
    result = subprocess.run(
        [
            ffmpeg_binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-framerate",
            str(frame_rate),
            "-f",
            "h264",
            "-i",
            str(source),
            "-c:v",
            "copy",
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            str(output),
        ],
        capture_output=True,
        check=False,
        timeout=30.0,
    )
    if result.returncode != 0:
        raise RuntimeError("FFmpeg could not remux H264 segment")


__all__ = [
    "ArchivedVideoSegment",
    "H264ArchiveRecorder",
    "MediaDownloadRegistry",
    "PublishedMedia",
    "RemoteVideoArchiveUploader",
    "RemoteVideoArchiveService",
    "VideoArchiveManifest",
    "VideoArchiveManifestEntry",
]
