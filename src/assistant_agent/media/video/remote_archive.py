"""Bounded H.264 segment archiving for the external visual Memory Service."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import logging
from pathlib import Path
import secrets
import sqlite3
import subprocess
from threading import BoundedSemaphore, Lock
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
        clock: Callable[[], float] = time.monotonic,
        max_mux_concurrency: int = 2,
    ) -> None:
        if segment_seconds <= 0:
            raise ValueError("segment_seconds must be positive")
        if max_mux_concurrency <= 0:
            raise ValueError("max_mux_concurrency must be positive")
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
        self._clock = clock
        self._locks_guard = Lock()
        self._session_locks: dict[str, Lock] = {}
        self._mux_slots = BoundedSemaphore(max_mux_concurrency)

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
        timestamp = self._clock()
        _ignored_timestamp, normalized_time = _capture_time(captured_at)
        completed: list[ArchivedVideoSegment] = []
        with self._session_lock(session_id):
            current = self._open.get(session_id)
            if (
                current is not None
                and timestamp - current.started_at_seconds >= self.segment_seconds
            ):
                previous = current
                current = self._start_segment(
                    session_id,
                    timestamp=timestamp,
                    start_time=normalized_time,
                    frame_rate=frame_rate,
                )
                with current.raw_path.open("ab") as handle:
                    handle.write(h264_bytes)
                try:
                    completed.append(self._finalize(previous))
                except Exception:
                    with previous.raw_path.open("ab") as handle:
                        handle.write(current.raw_path.read_bytes())
                    current.raw_path.unlink(missing_ok=True)
                    self._open[session_id] = previous
                    raise
                return tuple(completed)
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
        with self._session_lock(session_id):
            current = self._open.get(session_id)
            if current is None:
                return None
            segment = self._finalize(current)
            if self._open.get(session_id) is current:
                self._open.pop(session_id, None)
            return segment

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
        raw_path = directory / (
            f"segment-{sequence:06d}-{secrets.token_hex(8)}.h264.part"
        )
        current = _OpenSegment(
            sequence=sequence,
            raw_path=raw_path,
            started_at_seconds=timestamp,
            start_time=start_time,
            frame_rate=frame_rate if frame_rate > 0 else 25.0,
        )
        self._open[session_id] = current
        return current

    def _finalize(self, current: _OpenSegment) -> ArchivedVideoSegment:
        digest = hashlib.sha256(
            f"{current.raw_path}:{current.sequence}:{current.start_time}".encode(
                "utf-8"
            )
        ).hexdigest()[:24]
        file_id = f"video-{digest}"
        output_path = current.raw_path.with_name(f"{file_id}.mp4")
        partial_path = output_path.with_suffix(".mp4.part")
        try:
            with self._mux_slots:
                self._muxer(current.raw_path, partial_path, current.frame_rate)
            if not partial_path.is_file() or partial_path.stat().st_size == 0:
                raise RuntimeError("H264 muxer did not produce an MP4")
            partial_path.replace(output_path)
        except Exception:
            partial_path.unlink(missing_ok=True)
            raise
        current.raw_path.unlink(missing_ok=True)
        return ArchivedVideoSegment(
            file_id=file_id,
            path=output_path.resolve(),
            start_time=current.start_time,
        )

    def _session_lock(self, session_id: str) -> Lock:
        with self._locks_guard:
            return self._session_locks.setdefault(session_id, Lock())


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
        await self.preserve(segment, user_id=user_id, session_id=session_id)
        published: PublishedMedia | None = None
        try:
            published = await asyncio.to_thread(
                self.registry.publish,
                segment.path,
                file_id=segment.file_id,
            )
            await self._save_async(
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
                await self._save_async(
                    segment,
                    user_id=user_id,
                    session_id=session_id,
                    status="failed",
                )
                return "failed"
            await self._save_async(
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
                    await asyncio.to_thread(segment.path.unlink, missing_ok=True)
                    if self.manifest is not None:
                        await asyncio.to_thread(
                            self.manifest.remove,
                            segment.file_id,
                        )
                    return "completed"
                if status.status == "failed":
                    await self._save_async(
                        segment,
                        user_id=user_id,
                        session_id=session_id,
                        status="failed",
                    )
                    return "failed"
                await asyncio.sleep(self.poll_interval_seconds)
            await self._save_async(
                segment,
                user_id=user_id,
                session_id=session_id,
                status="timeout",
            )
            return "timeout"
        except Exception as exc:  # noqa: BLE001 - background dependency boundary.
            await self._save_async(
                segment,
                user_id=user_id,
                session_id=session_id,
                status="failed",
            )
            logger.warning(
                "remote_visual_memory_upload_failed file=%s error_type=%s",
                segment.file_id,
                type(exc).__name__,
            )
            return "failed"
        finally:
            if published is not None:
                await asyncio.to_thread(self.registry.revoke, published.token)

    async def preserve(
        self,
        segment: ArchivedVideoSegment,
        *,
        user_id: str,
        session_id: str,
    ) -> None:
        """Durably register a completed MP4 before background scheduling."""

        await self._save_async(
            segment,
            user_id=user_id,
            session_id=session_id,
            status="ready",
        )

    async def _save_async(
        self,
        segment: ArchivedVideoSegment,
        *,
        user_id: str,
        session_id: str,
        status: str,
    ) -> None:
        await asyncio.to_thread(
            self._save,
            segment,
            user_id=user_id,
            session_id=session_id,
            status=status,
        )

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
class _QueuedFrame:
    h264_bytes: bytes
    captured_at: str | None
    frame_rate: float


@dataclass(frozen=True)
class _RotateCommand:
    pass


@dataclass(frozen=True)
class _StopCommand:
    pass


_ROTATE = _RotateCommand()
_STOP = _StopCommand()


@dataclass
class _ArchiveConnection:
    user_id: str
    session_id: str
    queue: asyncio.Queue[_QueuedFrame | _RotateCommand | _StopCommand]
    worker: asyncio.Task[None] | None = None
    rotation_task: asyncio.Task[None] | None = None
    pending_bytes: int = 0


class RemoteVideoArchiveService:
    """Preserve per-connection frame order without blocking the WebSocket loop."""

    def __init__(
        self,
        *,
        recorder: H264ArchiveRecorder,
        uploader: RemoteVideoArchiveUploader,
        max_pending_bytes: int = 64 * 1024 * 1024,
        max_total_pending_bytes: int = 256 * 1024 * 1024,
        max_pending_items: int = 4_096,
        max_connections: int = 128,
    ) -> None:
        self.recorder = recorder
        self.uploader = uploader
        self.max_pending_bytes = max_pending_bytes
        self.max_total_pending_bytes = max_total_pending_bytes
        self.max_pending_items = max_pending_items
        self.max_connections = max_connections
        self._total_pending_bytes = 0
        self._connections: dict[str, _ArchiveConnection] = {}
        self._upload_tasks: set[asyncio.Task[str]] = set()

    def open_session(
        self,
        *,
        connection_id: str,
        user_id: str,
        session_id: str,
    ) -> bool:
        if connection_id in self._connections:
            raise ValueError("archive connection is already open")
        if len(self._connections) >= self.max_connections:
            return False
        connection = _ArchiveConnection(
            user_id=user_id,
            session_id=session_id,
            queue=asyncio.Queue(),
        )
        connection.worker = asyncio.create_task(
            self._run_connection(connection),
            name=f"remote-visual-memory-archive:{connection_id}",
        )
        self._connections[connection_id] = connection
        return True

    def enqueue_frame(
        self,
        *,
        connection_id: str,
        h264_bytes: bytes,
        captured_at: str | None,
        frame_rate: float,
    ) -> bool:
        return self.enqueue_frames(
            connection_id=connection_id,
            frames=((h264_bytes, captured_at, frame_rate),),
        )

    def enqueue_frames(
        self,
        *,
        connection_id: str,
        frames: Sequence[tuple[bytes, str | None, float]],
    ) -> bool:
        connection = self._connections.get(connection_id)
        if not frames:
            return True
        frame_size = sum(len(item[0]) for item in frames)
        if (
            connection is None
            or frame_size <= 0
            or connection.pending_bytes + frame_size > self.max_pending_bytes
            or self._total_pending_bytes + frame_size > self.max_total_pending_bytes
            or connection.queue.qsize() + len(frames) > self.max_pending_items
        ):
            return False
        connection.pending_bytes += frame_size
        self._total_pending_bytes += frame_size
        for h264_bytes, captured_at, frame_rate in frames:
            connection.queue.put_nowait(
                _QueuedFrame(
                    h264_bytes=h264_bytes,
                    captured_at=captured_at,
                    frame_rate=frame_rate,
                )
            )
        if connection.rotation_task is None:
            connection.rotation_task = asyncio.create_task(
                self._rotate_periodically(connection),
                name=f"remote-visual-memory-rotate:{connection_id}",
            )
        return True

    async def _run_connection(self, connection: _ArchiveConnection) -> None:
        while True:
            item = await connection.queue.get()
            try:
                if isinstance(item, _RotateCommand):
                    await self._flush_and_schedule(connection)
                    continue
                if isinstance(item, _StopCommand):
                    await self._flush_and_schedule(connection)
                    return
                frame = item
                segments = await asyncio.to_thread(
                    self.recorder.append,
                    session_id=connection.session_id,
                    h264_bytes=frame.h264_bytes,
                    captured_at=frame.captured_at,
                    frame_rate=frame.frame_rate,
                )
                for segment in segments:
                    await self._schedule_upload(segment, connection=connection)
            except Exception as exc:  # noqa: BLE001 - optional archive lane.
                logger.warning(
                    "remote_visual_memory_archive_failed error_type=%s",
                    type(exc).__name__,
                )
            finally:
                if isinstance(item, _QueuedFrame):
                    connection.pending_bytes -= len(item.h264_bytes)
                    self._total_pending_bytes -= len(item.h264_bytes)
                connection.queue.task_done()

    async def _flush_and_schedule(self, connection: _ArchiveConnection) -> None:
        try:
            segment = await asyncio.to_thread(
                self.recorder.flush,
                connection.session_id,
            )
            if segment is not None:
                await self._schedule_upload(segment, connection=connection)
        except Exception as exc:  # noqa: BLE001 - optional archive lane.
            logger.warning(
                "remote_visual_memory_flush_failed error_type=%s",
                type(exc).__name__,
            )

    async def _rotate_periodically(self, connection: _ArchiveConnection) -> None:
        try:
            while True:
                await asyncio.sleep(self.recorder.segment_seconds)
                connection.queue.put_nowait(_ROTATE)
        except asyncio.CancelledError:
            raise

    async def close_session(self, connection_id: str) -> None:
        connection = self._connections.pop(connection_id, None)
        if connection is None:
            return
        if connection.rotation_task is not None:
            connection.rotation_task.cancel()
            await asyncio.gather(connection.rotation_task, return_exceptions=True)
        connection.queue.put_nowait(_STOP)
        if connection.worker is not None:
            await asyncio.gather(connection.worker, return_exceptions=True)

    async def _schedule_upload(
        self,
        segment: ArchivedVideoSegment,
        *,
        connection: _ArchiveConnection,
    ) -> None:
        preserve = getattr(self.uploader, "preserve", None)
        if callable(preserve):
            await preserve(
                segment,
                user_id=connection.user_id,
                session_id=connection.session_id,
            )
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
        entries = await asyncio.to_thread(manifest.pending)
        for entry in entries:
            await self._schedule_upload(
                ArchivedVideoSegment(
                    file_id=entry.file_id,
                    path=entry.path,
                    start_time=entry.start_time,
                ),
                connection=_ArchiveConnection(
                    user_id=entry.user_id,
                    session_id=entry.session_id,
                    queue=asyncio.Queue(),
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
