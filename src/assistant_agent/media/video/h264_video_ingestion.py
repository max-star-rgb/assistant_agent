"""Bounded H.264 I-frame ingestion for the media agent-service entry."""

from __future__ import annotations

import hashlib
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any

from assistant_agent.media.video.video_context import VideoContextStore, VideoFrame


DEFAULT_MAX_FRAME_BYTES = 8 * 1024 * 1024
DEFAULT_DECODE_TIMEOUT_SECONDS = 3.0
DEFAULT_WINDOW_SIZE = 3
REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_FRAME_ROOT = REPO_ROOT / ".data" / "agent_service_video_frames"


@dataclass(frozen=True)
class DecodedFrameData:
    """Bounded local pixel data emitted with a decoded JPEG frame."""

    fingerprint: tuple[int, ...] = ()
    width: int = 0
    height: int = 0


FrameDecoder = Callable[[bytes, Path, float], DecodedFrameData | None]


class H264VideoIngestionError(ValueError):
    """Recoverable, prompt-safe media ingestion failure."""


class H264VideoIngestionService:
    """Decode independent Annex-B H.264 frames into a bounded JPEG context."""

    def __init__(
        self,
        *,
        store: VideoContextStore,
        root: Path | str = DEFAULT_FRAME_ROOT,
        decoder: FrameDecoder | None = None,
        window_size: int = DEFAULT_WINDOW_SIZE,
        max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
        decode_timeout_s: float = DEFAULT_DECODE_TIMEOUT_SECONDS,
        ffmpeg_binary: str = "/usr/bin/ffmpeg",
    ) -> None:
        if window_size <= 0:
            raise ValueError("window_size must be positive")
        if max_frame_bytes <= 0:
            raise ValueError("max_frame_bytes must be positive")
        if decode_timeout_s <= 0:
            raise ValueError("decode_timeout_s must be positive")
        self.store = store
        self.root = Path(root)
        self.window_size = window_size
        self.max_frame_bytes = max_frame_bytes
        self.decode_timeout_s = decode_timeout_s
        self._decoder = decoder or (
            lambda data, destination, timeout_s: _decode_h264_with_ffmpeg(
                data,
                destination,
                timeout_s,
                ffmpeg_binary=ffmpeg_binary,
            )
        )
        self._sequences: dict[str, int] = {}
        self._lock = Lock()

    def video_id_for_session(self, session_id: str) -> str:
        if not session_id.strip():
            raise H264VideoIngestionError("video session id is required")
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]
        return f"agent-service-video-{digest}"

    def ingest(
        self,
        session_id: str,
        frame_index: str,
        video_hex: str,
        video_config: dict[str, Any],
        timestamp: str | None,
    ) -> VideoFrame:
        h264_bytes = self._validated_h264_bytes(video_hex, video_config)
        video_id = self.video_id_for_session(session_id)

        with self._lock:
            sequence = self._sequences.get(video_id, 0) + 1
            self._sequences[video_id] = sequence
            video_dir = self.root / video_id.removeprefix("agent-service-video-")
            video_dir.mkdir(parents=True, exist_ok=True)
            destination = video_dir / f"frame-{sequence:06d}.jpg"
            try:
                decoded = self._decoder(h264_bytes, destination, self.decode_timeout_s)
            except subprocess.TimeoutExpired as exc:
                destination.unlink(missing_ok=True)
                raise H264VideoIngestionError("H264 frame decode timed out") from exc
            except H264VideoIngestionError:
                destination.unlink(missing_ok=True)
                raise
            except OSError as exc:
                destination.unlink(missing_ok=True)
                raise H264VideoIngestionError("H264 decoder unavailable") from exc
            except Exception as exc:
                destination.unlink(missing_ok=True)
                raise H264VideoIngestionError("H264 frame decode failed") from exc

            if not destination.is_file() or destination.stat().st_size == 0:
                destination.unlink(missing_ok=True)
                raise H264VideoIngestionError("H264 decoder did not produce a JPEG frame")

            before = self.store.get_recent_frames(video_id)
            frame = VideoFrame(
                video_id=video_id,
                frame_id=f"frame-{sequence:06d}",
                uri=str(destination.resolve()),
                sequence=sequence,
                timestamp_ms=_timestamp_ms(timestamp),
                metadata={
                    "source": "agent_service_websocket",
                    "frame_index": str(frame_index),
                    "codec": "H264",
                    "resolution": _optional_string(video_config.get("resolution")),
                    "frame_rate": video_config.get("frameRate"),
                },
                fingerprint=tuple(decoded.fingerprint) if decoded and decoded.fingerprint else None,
                fingerprint_width=decoded.width if decoded and decoded.width > 0 else None,
                fingerprint_height=decoded.height if decoded and decoded.height > 0 else None,
            )
            self.store.append_frame(frame)
            retained_uris = {
                retained.uri
                for retained in self.store.get_recent_frames(video_id, limit=self.window_size)
            }
            for evicted in before:
                if evicted.uri not in retained_uris:
                    Path(evicted.uri).unlink(missing_ok=True)
            return frame

    def cleanup(self, video_id: str) -> None:
        with self._lock:
            removed = self.store.remove_video(video_id)
            self._sequences.pop(video_id, None)
            for frame in removed:
                Path(frame.uri).unlink(missing_ok=True)
            video_dir = self.root / video_id.removeprefix("agent-service-video-")
            try:
                video_dir.rmdir()
            except OSError:
                pass

    def _validated_h264_bytes(self, video_hex: str, video_config: dict[str, Any]) -> bytes:
        codec = _optional_string(video_config.get("codec"))
        if codec is None or codec.upper() != "H264":
            raise H264VideoIngestionError("videoConfig.codec must be H264")
        normalized = video_hex.strip()
        if not normalized:
            raise H264VideoIngestionError("videoContent is empty")
        if re.fullmatch(r"[0-9a-fA-F]+", normalized) is None:
            raise H264VideoIngestionError("videoContent must be valid hexadecimal")
        if len(normalized) % 2:
            raise H264VideoIngestionError("videoContent must contain an even number of hexadecimal characters")
        byte_length = len(normalized) // 2
        if byte_length > self.max_frame_bytes:
            raise H264VideoIngestionError(
                f"videoContent exceeds {self.max_frame_bytes} bytes"
            )
        h264_bytes = bytes.fromhex(normalized)
        if not h264_bytes.startswith((b"\x00\x00\x01", b"\x00\x00\x00\x01")):
            raise H264VideoIngestionError("videoContent must use an Annex-B NAL start code")
        return h264_bytes


def _decode_h264_with_ffmpeg(
    h264_bytes: bytes,
    destination: Path,
    timeout_s: float,
    *,
    ffmpeg_binary: str,
) -> DecodedFrameData:
    fingerprint_width = 32
    fingerprint_height = 18
    filter_graph = (
        "[0:v]split=2[full][thumb];"
        f"[thumb]scale={fingerprint_width}:{fingerprint_height},format=gray[gray]"
    )
    result = subprocess.run(
        [
            ffmpeg_binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "h264",
            "-i",
            "pipe:0",
            "-filter_complex",
            filter_graph,
            "-map",
            "[full]",
            "-frames:v",
            "1",
            "-f",
            "image2",
            "-vcodec",
            "mjpeg",
            "-y",
            str(destination),
            "-map",
            "[gray]",
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "pipe:1",
        ],
        input=h264_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_s,
        check=False,
    )
    if result.returncode != 0:
        raise H264VideoIngestionError("FFmpeg could not decode the H264 frame")
    expected = fingerprint_width * fingerprint_height
    if len(result.stdout) < expected:
        raise H264VideoIngestionError("FFmpeg did not produce a complete frame fingerprint")
    return DecodedFrameData(
        fingerprint=tuple(result.stdout[:expected]),
        width=fingerprint_width,
        height=fingerprint_height,
    )


def _timestamp_ms(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def _optional_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None
