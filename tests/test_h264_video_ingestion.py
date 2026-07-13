from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from assistant_agent.services.h264_video_ingestion import (
    H264VideoIngestionError,
    H264VideoIngestionService,
)
from assistant_agent.services.video_context import InMemoryVideoContextStore


VALID_H264_HEX = "0000000167aa0000000168bb0000000165cc"


def test_ingest_decodes_hex_registers_jpeg_and_uses_opaque_video_id(tmp_path: Path) -> None:
    store = InMemoryVideoContextStore(window_size=3)
    calls: list[tuple[bytes, Path, float]] = []

    def decoder(data: bytes, destination: Path, timeout_s: float) -> None:
        calls.append((data, destination, timeout_s))
        destination.write_bytes(b"\xff\xd8jpeg\xff\xd9")

    service = H264VideoIngestionService(store=store, root=tmp_path, decoder=decoder)
    frame = service.ingest(
        session_id="../../../private-session",
        frame_index="7",
        video_hex=VALID_H264_HEX,
        video_config={"codec": "H264", "resolution": "1280x720", "frameRate": 25},
        timestamp="2026-07-13T08:30:00Z",
    )

    assert calls[0][0].startswith(b"\x00\x00\x00\x01")
    assert frame.video_id == service.video_id_for_session("../../../private-session")
    assert "private-session" not in frame.uri
    assert Path(frame.uri).read_bytes().startswith(b"\xff\xd8")
    assert frame.sequence == 1
    assert frame.timestamp_ms == 1783931400000
    assert frame.metadata == {
        "source": "agent_service_websocket",
        "frame_index": "7",
        "codec": "H264",
        "resolution": "1280x720",
        "frame_rate": 25,
    }
    assert store.get_recent_frames(frame.video_id) == [frame]


@pytest.mark.parametrize(
    ("video_hex", "config", "message"),
    [
        ("xyz", {"codec": "H264"}, "valid hexadecimal"),
        ("001", {"codec": "H264"}, "even number"),
        ("00112233", {"codec": "H264"}, "Annex-B"),
        (VALID_H264_HEX, {"codec": "VP8"}, "codec must be H264"),
        ("", {"codec": "H264"}, "videoContent is empty"),
    ],
)
def test_ingest_rejects_invalid_transport(
    video_hex: str,
    config: dict,
    message: str,
    tmp_path: Path,
) -> None:
    service = H264VideoIngestionService(
        store=InMemoryVideoContextStore(),
        root=tmp_path,
        decoder=lambda *_: None,
    )

    with pytest.raises(H264VideoIngestionError, match=message):
        service.ingest("s1", "1", video_hex, config, None)


def test_ingest_rejects_frame_over_byte_limit(tmp_path: Path) -> None:
    service = H264VideoIngestionService(
        store=InMemoryVideoContextStore(),
        root=tmp_path,
        decoder=lambda *_: None,
        max_frame_bytes=4,
    )

    with pytest.raises(H264VideoIngestionError, match="exceeds 4 bytes"):
        service.ingest("s1", "1", VALID_H264_HEX, {"codec": "H264"}, None)


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (subprocess.TimeoutExpired(cmd="ffmpeg", timeout=2), "timed out"),
        (OSError("decoder unavailable at /private/path"), "decoder unavailable"),
    ],
)
def test_ingest_maps_decoder_failures_to_prompt_safe_error(
    error: Exception,
    message: str,
    tmp_path: Path,
) -> None:
    def failing_decoder(*_args) -> None:
        raise error

    service = H264VideoIngestionService(
        store=InMemoryVideoContextStore(),
        root=tmp_path,
        decoder=failing_decoder,
    )

    with pytest.raises(H264VideoIngestionError, match=message):
        service.ingest("s1", "1", VALID_H264_HEX, {"codec": "H264"}, None)


def test_ingest_rejects_empty_decoder_output(tmp_path: Path) -> None:
    service = H264VideoIngestionService(
        store=InMemoryVideoContextStore(),
        root=tmp_path,
        decoder=lambda *_: None,
    )

    with pytest.raises(H264VideoIngestionError, match="did not produce a JPEG"):
        service.ingest("s1", "1", VALID_H264_HEX, {"codec": "H264"}, None)


def test_ingest_evicts_oldest_frame_and_artifact(tmp_path: Path) -> None:
    store = InMemoryVideoContextStore(window_size=3)

    def decoder(_data: bytes, destination: Path, _timeout_s: float) -> None:
        destination.write_bytes(b"\xff\xd8jpeg\xff\xd9")

    service = H264VideoIngestionService(
        store=store,
        root=tmp_path,
        decoder=decoder,
        window_size=3,
    )
    frames = [
        service.ingest("s1", str(index), VALID_H264_HEX, {"codec": "H264"}, None)
        for index in range(1, 5)
    ]

    assert not Path(frames[0].uri).exists()
    assert all(Path(frame.uri).exists() for frame in frames[1:])
    assert store.get_recent_frames(frames[-1].video_id) == frames[1:]


def test_cleanup_deletes_retained_artifacts_and_context(tmp_path: Path) -> None:
    store = InMemoryVideoContextStore(window_size=3)

    def decoder(_data: bytes, destination: Path, _timeout_s: float) -> None:
        destination.write_bytes(b"\xff\xd8jpeg\xff\xd9")

    service = H264VideoIngestionService(store=store, root=tmp_path, decoder=decoder)
    frame = service.ingest("s1", "1", VALID_H264_HEX, {"codec": "H264"}, None)

    service.cleanup(frame.video_id)

    assert not Path(frame.uri).exists()
    assert store.get_recent_frames(frame.video_id) == []
    service.cleanup(frame.video_id)
