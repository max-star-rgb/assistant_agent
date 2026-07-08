from pathlib import Path

import pytest

from assistant_agent.video_ai.app import RealtimeVideoUnderstandingApp
from assistant_agent.video_ai.detection.semantic_detector import MetadataEmbeddingModel, SemanticChangeDetector
from assistant_agent.video_ai.keyframe.selector import KeyframeSelectorConfig
from assistant_agent.video_ai.keyframe.storage import FileKeyframeStorage, NoopKeyframeStorage
from assistant_agent.video_ai.qwen.vision_client import MockQwenVisionClient, VisionObservation
from assistant_agent.video_ai.sampling.adaptive_sampler import AdaptiveSamplerConfig
from assistant_agent.video_ai.types import VideoFrame


pytestmark = pytest.mark.fast


def test_static_room_throttles_qwen_calls_and_logs_sampling_rate() -> None:
    client = MockQwenVisionClient()
    app = _app(client, max_interval_seconds=5.0)

    results = [
        app.process_frame(_frame(f"still_{index}", index / 5.0, 12, embedding=[1.0, 0.0]))
        for index in range(31)
    ]

    assert client.understand_calls <= 2
    assert sum(result.keyframe_selected for result in results) <= 2
    assert sum(not result.sampled for result in results) >= 20
    assert app.log_records[-1]["sampling_rate"] == 0.2
    assert {"timestamp", "frame_id", "sampling_rate", "change_score", "keyframe_selected", "qwen_called", "latency_ms"} <= set(
        app.log_records[-1]
    )


def test_person_entering_room_creates_keyframe_and_updates_state() -> None:
    client = MockQwenVisionClient()
    app = _app(client)

    for index in range(5):
        app.process_frame(_frame(f"empty_{index}", index / 5.0, 8, embedding=[1.0, 0.0]))

    response = VisionObservation(
        scene="room",
        objects=["table"],
        people=["person"],
        actions=["person entered room"],
        changes_from_previous="A person entered the room.",
        important_events=["person entered room"],
        summary="A person is now standing beside the table.",
    )
    result = app.process_frame(
        _frame("person_entered", 1.0, 120, embedding=[0.2, 0.9], qwen_response=response)
    )

    assert result.keyframe_selected is True
    assert result.qwen_called is True
    assert client.understand_calls >= 2
    assert "person" in app.memory.current_state
    assert any("person entered room" in event.event for event in app.memory.events)


def test_semantic_object_change_selects_keyframe_even_when_pixel_delta_is_small() -> None:
    client = MockQwenVisionClient()
    app = _app(client, threshold=0.45)

    app.process_frame(_frame("empty_table", 0.0, 20, embedding=[1.0, 0.0]))

    response = VisionObservation(
        scene="table",
        objects=["cup"],
        people=[],
        actions=["cup appeared on table"],
        changes_from_previous="A cup appeared on the table.",
        important_events=["cup appeared"],
        summary="A cup is visible on the table.",
    )
    result = app.process_frame(
        _frame("cup_appeared", 1.0, 22, embedding=[0.0, 1.0], qwen_response=response)
    )

    assert result.metrics.pixel_change_score < 0.05
    assert result.metrics.semantic_change_score > 0.75
    assert result.keyframe_selected is True
    assert "cup" in app.memory.current_state


def test_query_answering_uses_memory_and_recent_keyframes_without_rescanning_video() -> None:
    client = MockQwenVisionClient()
    app = _app(client)

    app.process_frame(
        _frame(
            "person_with_cup",
            0.0,
            80,
            embedding=[0.1, 0.9],
            qwen_response=VisionObservation(
                scene="room",
                objects=["cup"],
                people=["person"],
                actions=["person holding cup"],
                changes_from_previous="A person is holding a cup.",
                important_events=["person picked up cup"],
                summary="A person is holding a cup.",
            ),
        )
    )
    understand_calls = client.understand_calls

    answer = app.answer_query("刚才谁拿走了杯子？")

    assert client.understand_calls == understand_calls
    assert client.answer_calls == 1
    assert "cup" in answer.answer
    assert answer.memory_state["keyframes"]


def test_selected_keyframe_is_persisted_when_frame_uri_is_not_usable(tmp_path: Path) -> None:
    client = MockQwenVisionClient()
    app = RealtimeVideoUnderstandingApp(
        qwen_client=client,
        semantic_detector=SemanticChangeDetector(MetadataEmbeddingModel()),
        keyframe_storage=FileKeyframeStorage(tmp_path),
    )

    result = app.process_frame(_frame("first", 0.0, 33, embedding=[1.0, 0.0]))

    keyframe_uri = app.memory.keyframes[-1].uri
    assert result.keyframe_selected is True
    assert keyframe_uri is not None
    assert Path(keyframe_uri).exists()
    assert Path(keyframe_uri).suffix == ".pgm"


def _app(
    client: MockQwenVisionClient,
    *,
    threshold: float = 0.35,
    max_interval_seconds: float = 5.0,
) -> RealtimeVideoUnderstandingApp:
    return RealtimeVideoUnderstandingApp(
        qwen_client=client,
        semantic_detector=SemanticChangeDetector(MetadataEmbeddingModel()),
        sampler_config=AdaptiveSamplerConfig(
            base_input_fps=5.0,
            still_fps=0.2,
            normal_fps=1.0,
            active_fps=5.0,
            burst_fps=5.0,
            still_threshold=0.05,
            active_threshold=0.18,
            burst_threshold=0.65,
            burst_duration_seconds=2.0,
        ),
        keyframe_config=KeyframeSelectorConfig(
            threshold=threshold,
            min_interval_seconds=0.5,
            max_interval_seconds=max_interval_seconds,
        ),
        keyframe_storage=NoopKeyframeStorage(),
    )


def _frame(
    frame_id: str,
    timestamp_seconds: float,
    intensity: int,
    *,
    embedding: list[float],
    qwen_response: VisionObservation | None = None,
) -> VideoFrame:
    metadata = {"embedding": embedding}
    if qwen_response is not None:
        metadata["qwen_response"] = qwen_response
    pixels = [[intensity for _ in range(8)] for _ in range(8)]
    return VideoFrame(
        frame_id=frame_id,
        timestamp_seconds=timestamp_seconds,
        pixels=pixels,
        uri=f"memory://{frame_id}.jpg",
        metadata=metadata,
    )
