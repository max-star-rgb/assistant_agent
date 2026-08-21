from __future__ import annotations

import asyncio
from pathlib import Path

from assistant_agent.agent_server.media_app import _VisualPerceptionConnection
from assistant_agent.media.video.h264_video_ingestion import (
    DecodedFrameData,
    H264VideoIngestionService,
)
from assistant_agent.media.video.video_context import (
    InMemoryVideoContextStore,
    VideoFrame,
)
from assistant_agent.media.visual_perception.module import VisualPerceptionSession


class RecordingObserver:
    def __init__(self) -> None:
        self.promote_window_called = False

    async def submit(self, _frame: VideoFrame) -> None:
        return None

    async def promote_window(self, frames: tuple[VideoFrame, ...], **_metadata) -> None:
        del frames
        self.promote_window_called = True
        raise AssertionError("chat freeze must not start or replay VLM observations")

    async def close(self) -> None:
        return None


def test_video_ingress_replaces_stale_pending_work_with_the_latest_frame() -> None:
    async def exercise() -> None:
        release = asyncio.Event()
        connection = _VisualPerceptionConnection()
        executed: list[int] = []

        def operation(sequence: int):
            async def run() -> bool:
                executed.append(sequence)
                if sequence == 1:
                    await release.wait()
                return True

            return run

        try:
            connection.enqueue_video(operation(1))
            await asyncio.sleep(0)
            for sequence in range(2, 10):
                connection.enqueue_video(operation(sequence))

            release.set()
            assert await connection.wait_for_latest_video() is True
            assert executed == [1, 9]
        finally:
            release.set()
            await connection.aclose_video_tasks()

    asyncio.run(exercise())


def test_failed_latest_video_ingestion_fails_closed() -> None:
    async def exercise() -> None:
        connection = _VisualPerceptionConnection()

        async def fail() -> bool:
            return False

        try:
            connection.enqueue_video(fail)
            assert await connection.wait_for_latest_video() is False
        finally:
            await connection.aclose_video_tasks()

    asyncio.run(exercise())


def test_chat_only_freezes_the_latest_eight_decoded_frame_boundaries() -> None:
    """Regression: chat-time promotion starts VLM after the user has already asked."""

    store = InMemoryVideoContextStore(window_size=8)
    observer = RecordingObserver()
    video_id = "video-window"
    for sequence in range(1, 12):
        store.append_frame(
            VideoFrame(
                video_id=video_id,
                frame_id=f"frame-{sequence}",
                uri=f"/frames/{sequence}.jpg",
                sequence=sequence,
            )
        )

    session = VisualPerceptionSession(
        observer=observer,
        video_context_store=store,
        release=lambda _session: None,
    )

    window = asyncio.run(
        session.prepare_strict_window(["missing-video", video_id])
    )

    assert window is not None
    assert window.video_id == video_id
    assert window.start_sequence == 4
    assert window.target_sequence == 11
    assert window.sequences == (4, 5, 6, 7, 8, 9, 10, 11)
    assert window.window_id.startswith("visual-window-")
    assert observer.promote_window_called is False


def test_chat_uses_all_frames_when_fewer_than_eight_exist() -> None:
    """Regression: a short new call must still expose its complete decoded window."""

    store = InMemoryVideoContextStore(window_size=8)
    observer = RecordingObserver()
    for sequence in range(1, 4):
        store.append_frame(
            VideoFrame(
                video_id="short-video",
                frame_id=f"frame-{sequence}",
                uri=f"/frames/{sequence}.jpg",
                sequence=sequence,
            )
        )
    session = VisualPerceptionSession(
        observer=observer,
        video_context_store=store,
        release=lambda _session: None,
    )

    window = asyncio.run(session.prepare_strict_window(["short-video"]))

    assert window is not None
    assert window.start_sequence == 1
    assert window.target_sequence == 3
    assert window.sequences == (1, 2, 3)


def test_default_h264_retention_keeps_the_latest_eight_frames(tmp_path: Path) -> None:
    """Regression: chat-time context must retain exactly the latest eight frames."""

    store = InMemoryVideoContextStore()

    def decode(
        _payload: bytes,
        destination: Path,
        _timeout_seconds: float,
    ) -> DecodedFrameData:
        destination.write_bytes(b"jpeg")
        return DecodedFrameData()

    ingestion = H264VideoIngestionService(
        store=store,
        root=tmp_path,
        decoder=decode,
    )
    video_id = ingestion.video_id_for_session("session-window")

    frames = [
        ingestion.ingest(
            "session-window",
            f"index-{sequence}",
            "00000165",
            {"codec": "H264"},
            None,
        )
        for sequence in range(1, 12)
    ]

    assert [frame.sequence for frame in store.get_recent_frames(video_id)] == [
        4,
        5,
        6,
        7,
        8,
        9,
        10,
        11,
    ]
    assert all(Path(frame.uri).exists() for frame in frames[3:])
    assert all(not Path(frame.uri).exists() for frame in frames[:3])
