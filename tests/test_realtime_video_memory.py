from __future__ import annotations

import importlib

from assistant_agent.schemas.perception import VideoUnderstandingResult
from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig


def _result(*, summary: str, objects: list[str]) -> VideoUnderstandingResult:
    return VideoUnderstandingResult(
        summary=summary,
        objects=objects,
        provider="test-video",
        model="test-model",
        output_ref="provider://video/test/result",
    )


def test_video_memory_is_isolated_and_latest_failure_is_not_healthy() -> None:
    module = importlib.import_module("assistant_agent.services.realtime_video_memory")
    store = module.RealtimeVideoMemoryStore(max_keyframes=2)
    frame = module.SemanticKeyframeRecord(
        frame_id="frame-1",
        uri="/tmp/1.jpg",
        sequence=1,
        timestamp_ms=1000,
    )
    store.record_success("video-a", frame, _result(summary="cup", objects=["cup"]))

    assert store.snapshot("video-a").healthy is True
    assert store.snapshot("video-b") is None

    store.record_failure(
        "video-a",
        frame,
        {"code": "provider_timeout", "message": "timed out", "recoverable": True},
    )
    snapshot = store.snapshot("video-a")
    assert snapshot is not None
    assert snapshot.current_state == "cup"
    assert snapshot.objects == ["cup"]
    assert snapshot.healthy is False
    assert snapshot.last_error == {
        "code": "provider_timeout",
        "message": "timed out",
        "recoverable": True,
    }


def test_video_memory_trims_keyframes_and_returns_evicted_records() -> None:
    module = importlib.import_module("assistant_agent.services.realtime_video_memory")
    store = module.RealtimeVideoMemoryStore(max_keyframes=2)
    records = [
        module.SemanticKeyframeRecord(
            frame_id=f"frame-{sequence}",
            uri=f"/tmp/{sequence}.jpg",
            sequence=sequence,
            timestamp_ms=sequence * 1000,
        )
        for sequence in range(1, 4)
    ]

    assert store.record_success("video-a", records[0], _result(summary="1", objects=[])) == []
    assert store.record_success("video-a", records[1], _result(summary="2", objects=[])) == []
    assert store.record_success("video-a", records[2], _result(summary="3", objects=[])) == [records[0]]
    assert store.snapshot("video-a").keyframes == records[1:]


def test_video_memory_pending_state_does_not_invalidate_prior_success() -> None:
    module = importlib.import_module("assistant_agent.services.realtime_video_memory")
    store = module.RealtimeVideoMemoryStore()
    frame = module.SemanticKeyframeRecord(
        frame_id="frame-1", uri="/tmp/1.jpg", sequence=1, timestamp_ms=1000
    )
    store.record_success("video-a", frame, _result(summary="ready", objects=["cup"]))

    store.mark_pending("video-a", pending_count=1, in_flight=True)

    snapshot = store.snapshot("video-a")
    assert snapshot is not None
    assert snapshot.healthy is True
    assert snapshot.pending_count == 1
    assert snapshot.in_flight is True


def test_runtime_shares_one_video_memory_store_with_default_tool() -> None:
    runtime = AgentGraphRuntime(config=ProviderConfig())

    tool = runtime.registry.get("video_understanding")
    assert runtime.realtime_video_memory_store is tool.memory_store
