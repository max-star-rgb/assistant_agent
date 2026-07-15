from __future__ import annotations

import importlib

from assistant_agent.schemas.perception import VideoUnderstandingResult
from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.services.realtime_video_memory import project_realtime_video_context


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


def test_video_memory_retains_successful_observation_diagnostics() -> None:
    module = importlib.import_module("assistant_agent.services.realtime_video_memory")
    store = module.RealtimeVideoMemoryStore()
    frame = module.SemanticKeyframeRecord(
        frame_id="frame-1", uri="/tmp/1.jpg", sequence=1, timestamp_ms=1000
    )
    diagnostics = module.RealtimeVideoObservationDiagnostics(
        h264_decode_latency_ms=4,
        keyframe_selection_latency_ms=2,
        queue_wait_latency_ms=7,
        observation_latency_ms=80,
        published_at_ms=10_000,
    )

    store.record_success(
        "video-a",
        frame,
        _result(summary="ready", objects=["cup"]),
        diagnostics=diagnostics,
    )
    store.mark_pending("video-a", pending_count=1, in_flight=True)
    store.record_failure(
        "video-a",
        frame,
        {"code": "provider_timeout", "message": "timed out", "recoverable": True},
    )

    snapshot = store.snapshot("video-a")
    assert snapshot is not None
    assert snapshot.observation_diagnostics == diagnostics


def test_runtime_shares_one_video_memory_store_with_default_tool() -> None:
    runtime = AgentGraphRuntime(config=ProviderConfig())

    tool = runtime.registry.get("video_understanding")
    assert runtime.realtime_video_memory_store is tool.memory_store


def test_realtime_video_context_projects_ready_refreshing_and_failed_states() -> None:
    module = importlib.import_module("assistant_agent.services.realtime_video_memory")
    store = module.RealtimeVideoMemoryStore()
    frame = module.SemanticKeyframeRecord(
        frame_id="frame-3", uri="/private/frame.jpg", sequence=3, timestamp_ms=3000
    )
    diagnostics = module.RealtimeVideoObservationDiagnostics(
        observation_latency_ms=81,
        published_at_ms=10_000,
    )
    store.record_success(
        "video-a",
        frame,
        VideoUnderstandingResult(
            summary="A person lifts a red cup.",
            objects=["red cup"],
            people=["one person"],
            actions=["lifting a cup"],
            events=["cup lifted"],
            scene="desk",
            provider="qwen",
            model="qwen-vl-max",
            output_ref="provider://video/test/result",
        ),
        diagnostics=diagnostics,
    )

    ready = project_realtime_video_context(store.snapshot("video-a"), now_ms=10_120)
    assert ready.status == "ready"
    assert ready.summary == "A person lifts a red cup."
    assert ready.snapshot_sequence == 3
    assert ready.snapshot_age_ms == 7_120
    assert ready.frame_capture_age_ms == 7_120
    assert ready.snapshot_publish_age_ms == 120
    assert ready.observation_latency_ms == 81

    store.mark_pending("video-a", pending_count=1, in_flight=True)
    refreshing = project_realtime_video_context(store.snapshot("video-a"), now_ms=10_140)
    assert refreshing.status == "refreshing"
    assert refreshing.pending_count == 1
    assert refreshing.in_flight is True

    failed_store = module.RealtimeVideoMemoryStore()
    failed_store.record_failure(
        "video-b",
        frame,
        {"code": "provider_timeout", "message": "secret provider detail", "recoverable": True},
    )
    failed = project_realtime_video_context(failed_store.snapshot("video-b"), now_ms=10_200)
    assert failed.status == "failed"
    assert failed.summary == ""
    assert failed.error_code == "provider_timeout"
    assert "secret provider detail" not in failed.model_dump_json()
    assert "/private/frame.jpg" not in ready.model_dump_json()


def test_realtime_video_context_projects_capture_age_and_publish_age() -> None:
    module = importlib.import_module("assistant_agent.services.realtime_video_memory")
    store = module.RealtimeVideoMemoryStore()
    store.record_success(
        "video-a",
        module.SemanticKeyframeRecord(
            frame_id="frame-1",
            uri="/tmp/frame-1.jpg",
            sequence=1,
            timestamp_ms=10_000,
        ),
        _result(summary="ready", objects=["cup"]),
        diagnostics=module.RealtimeVideoObservationDiagnostics(published_at_ms=12_000),
    )

    context = project_realtime_video_context(store.snapshot("video-a"), now_ms=15_000)

    assert context.snapshot_age_ms == 5_000
    assert context.frame_capture_age_ms == 5_000
    assert context.snapshot_publish_age_ms == 3_000


def test_realtime_video_context_missing_capture_timestamp_does_not_invent_capture_age() -> None:
    module = importlib.import_module("assistant_agent.services.realtime_video_memory")
    store = module.RealtimeVideoMemoryStore()
    store.record_success(
        "video-a",
        module.SemanticKeyframeRecord(
            frame_id="frame-1",
            uri="/tmp/frame-1.jpg",
            sequence=1,
            timestamp_ms=None,
        ),
        _result(summary="ready", objects=["cup"]),
        diagnostics=module.RealtimeVideoObservationDiagnostics(published_at_ms=12_000),
    )

    context = project_realtime_video_context(store.snapshot("video-a"), now_ms=15_000)

    assert context.frame_capture_age_ms is None
    assert context.snapshot_publish_age_ms == 3_000
    assert context.snapshot_age_ms == 3_000


def test_realtime_video_context_future_capture_timestamp_does_not_invent_capture_age() -> None:
    module = importlib.import_module("assistant_agent.services.realtime_video_memory")
    store = module.RealtimeVideoMemoryStore()
    store.record_success(
        "video-a",
        module.SemanticKeyframeRecord(
            frame_id="frame-1",
            uri="/tmp/frame-1.jpg",
            sequence=1,
            timestamp_ms=20_000,
        ),
        _result(summary="ready", objects=["cup"]),
        diagnostics=module.RealtimeVideoObservationDiagnostics(published_at_ms=12_000),
    )

    context = project_realtime_video_context(store.snapshot("video-a"), now_ms=15_000)

    assert context.frame_capture_age_ms is None
    assert context.snapshot_publish_age_ms == 3_000
    assert context.snapshot_age_ms == 3_000


def test_realtime_video_context_projects_target_sequence_gap() -> None:
    module = importlib.import_module("assistant_agent.services.realtime_video_memory")
    store = module.RealtimeVideoMemoryStore()
    store.record_success(
        "video-a",
        module.SemanticKeyframeRecord(
            frame_id="frame-3",
            uri="/tmp/frame-3.jpg",
            sequence=3,
        ),
        _result(summary="ready", objects=["cup"]),
    )

    context = project_realtime_video_context(
        store.snapshot("video-a"),
        now_ms=15_000,
        target_sequence=5,
    )

    assert context.target_sequence == 5
    assert context.sequence_gap == 2


def test_realtime_video_context_distinguishes_pending_stale_and_unavailable() -> None:
    module = importlib.import_module("assistant_agent.services.realtime_video_memory")
    pending_store = module.RealtimeVideoMemoryStore()
    pending_store.mark_pending("video-a", pending_count=1, in_flight=False)
    pending = project_realtime_video_context(pending_store.snapshot("video-a"), now_ms=50_000)
    assert pending.status == "pending"

    frame = module.SemanticKeyframeRecord(
        frame_id="frame-1", uri="/tmp/frame.jpg", sequence=1, timestamp_ms=1000
    )
    stale_store = module.RealtimeVideoMemoryStore()
    stale_store.record_success(
        "video-a",
        frame,
        _result(summary="old scene", objects=["cup"]),
        diagnostics=module.RealtimeVideoObservationDiagnostics(published_at_ms=1_000),
    )
    stale_store.record_failure(
        "video-a",
        frame,
        {"code": "provider_timeout", "message": "timed out", "recoverable": True},
    )
    stale = project_realtime_video_context(stale_store.snapshot("video-a"), now_ms=2_000)
    assert stale.status == "stale"
    assert stale.summary == "old scene"
    assert project_realtime_video_context(None, now_ms=2_000).status == "unavailable"


def test_realtime_video_context_projection_is_bounded_to_two_thousand_chars() -> None:
    module = importlib.import_module("assistant_agent.services.realtime_video_memory")
    store = module.RealtimeVideoMemoryStore(max_events=50)
    frame = module.SemanticKeyframeRecord(
        frame_id="frame-1", uri="/tmp/frame.jpg", sequence=1, timestamp_ms=1000
    )
    store.record_success(
        "video-a",
        frame,
        VideoUnderstandingResult(
            summary="场景" * 1000,
            objects=[f"object-{index}-" + "x" * 200 for index in range(30)],
            people=[f"person-{index}-" + "x" * 200 for index in range(20)],
            actions=[f"action-{index}-" + "x" * 200 for index in range(30)],
            events=[f"event-{index}-" + "x" * 200 for index in range(30)],
            scene="scene" * 200,
            provider="qwen",
            model="qwen-vl-max",
            output_ref="provider://video/result",
        ),
    )

    context = project_realtime_video_context(store.snapshot("video-a"), now_ms=2_000)

    assert len(context.model_dump_json()) <= 2_000
