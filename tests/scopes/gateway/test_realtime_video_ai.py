import importlib
from pathlib import Path

import pytest

from assistant_agent.video_ai.app import RealtimeVideoUnderstandingApp
from assistant_agent.video_ai.detection.frame_difference import FrameDifferenceDetector
from assistant_agent.video_ai.detection.semantic_detector import MetadataEmbeddingModel, SemanticChangeDetector
from assistant_agent.video_ai.detection.vision_embedding_provider import VisionEmbeddingResult
from assistant_agent.video_ai.keyframe.selector import KeyframeSelectorConfig
from assistant_agent.video_ai.keyframe.storage import FileKeyframeStorage, NoopKeyframeStorage
from assistant_agent.video_ai.memory.state_manager import KeyframeMemoryRecord
from assistant_agent.video_ai.local_vision_client import (
    MockRealtimeVisionClient,
    VisionObservation,
    _keyframe_prompt,
)
from assistant_agent.video_ai.sampling.adaptive_sampler import AdaptiveSamplerConfig
from assistant_agent.video_ai.types import VideoFrame


pytestmark = pytest.mark.fast


def test_local_realtime_prompt_contains_lowercase_json_for_response_format() -> None:
    prompt = _keyframe_prompt("", [])

    assert "json" in prompt


def test_local_keyframe_prompt_does_not_include_local_image_uri(tmp_path: Path) -> None:
    local_uri = str(tmp_path / "private-frame.jpg")
    record = KeyframeMemoryRecord(
        frame_id="frame-1",
        timestamp_seconds=1.0,
        uri=local_uri,
        summary="红色方块",
        scene="测试场景",
        objects=["方块"],
        people=[],
    )

    prompt = _keyframe_prompt("当前状态", [record])

    assert local_uri not in prompt
    assert "frame-1" in prompt


def test_uri_text_is_not_used_as_frame_pixels() -> None:
    detector = FrameDifferenceDetector(fingerprint_size=(2, 2))
    left = VideoFrame(frame_id="1", timestamp_seconds=0.0, uri="/a.jpg")
    right = VideoFrame(frame_id="2", timestamp_seconds=1.0, uri="/very/different/name.jpg")

    assert detector.compare(right, left).pixel_change_score == 0.0


def test_adaptive_collector_selects_without_mllm_call() -> None:
    module = importlib.import_module("assistant_agent.video_ai.keyframe.collector")
    collector = module.AdaptiveKeyframeCollector(
        semantic_detector=SemanticChangeDetector(MetadataEmbeddingModel()),
        keyframe_config=KeyframeSelectorConfig(min_interval_seconds=0.5),
    )

    first = collector.collect(_frame("first", 0.0, 10, embedding=[1.0, 0.0]))
    still = collector.collect(_frame("still", 0.2, 10, embedding=[1.0, 0.0]))

    assert first.selected_frame is not None
    assert first.processing.keyframe_selected is True
    assert first.processing.qwen_called is False
    assert still.selected_frame is None


def test_static_room_throttles_vision_calls_and_logs_sampling_rate() -> None:
    client = MockRealtimeVisionClient()
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
    client = MockRealtimeVisionClient()
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
    client = MockRealtimeVisionClient()
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
    client = MockRealtimeVisionClient()
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
    client = MockRealtimeVisionClient()
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


def test_static_scene_does_not_trigger_gated_vision_embedding_provider() -> None:
    client = MockRealtimeVisionClient()
    embeddings = RecordingEmbeddingProvider(
        {
            "first": [1.0, 0.0],
            "still_1": [1.0, 0.0],
            "still_2": [1.0, 0.0],
        }
    )
    app = _app(
        client,
        semantic_detector=SemanticChangeDetector(embeddings, requires_visual_gate=True),
        max_interval_seconds=5.0,
    )

    app.process_frame(_frame("first", 0.0, 20, embedding=[1.0, 0.0]))
    app.process_frame(_frame("still_1", 1.0, 20, embedding=[1.0, 0.0]))
    result = app.process_frame(_frame("still_2", 2.0, 20, embedding=[1.0, 0.0]))

    assert embeddings.calls == []
    assert result.metrics.semantic_change_score == 0.0
    assert result.keyframe_selected is False


def test_visual_candidate_triggers_embedding_and_low_similarity_selects_keyframe() -> None:
    client = MockRealtimeVisionClient()
    embeddings = RecordingEmbeddingProvider(
        {
            "empty": [1.0, 0.0],
            "changed": [0.0, 1.0],
        }
    )
    app = _app(
        client,
        semantic_detector=SemanticChangeDetector(embeddings, requires_visual_gate=True),
        threshold=0.45,
    )

    app.process_frame(_frame("empty", 0.0, 20, embedding=[1.0, 0.0]))
    result = app.process_frame(_frame("changed", 1.0, 220, embedding=[0.0, 1.0]))

    assert embeddings.calls == ["changed", "empty"]
    assert result.metrics.pixel_change_score >= 0.45 or result.metrics.structural_change_score >= 0.45
    assert result.metrics.semantic_change_score == pytest.approx(1.0)
    assert result.keyframe_selected is True
    assert result.qwen_called is True


def test_selected_keyframe_embedding_is_cached_for_next_semantic_comparison() -> None:
    client = MockRealtimeVisionClient()
    embeddings = RecordingEmbeddingProvider(
        {
            "empty": [1.0, 0.0],
            "person": [0.0, 1.0],
            "door": [0.7, 0.7],
        }
    )
    app = _app(
        client,
        semantic_detector=SemanticChangeDetector(embeddings, requires_visual_gate=True),
        threshold=0.35,
    )

    app.process_frame(_frame("empty", 0.0, 20, embedding=[1.0, 0.0]))
    first_change = app.process_frame(_frame("person", 1.0, 220, embedding=[0.0, 1.0]))
    second_change = app.process_frame(_frame("door", 2.0, 30, embedding=[0.7, 0.7]))

    assert first_change.keyframe_selected is True
    assert second_change.metrics.semantic_change_score > 0.0
    assert embeddings.calls.count("person") == 1
    assert embeddings.calls == ["person", "empty", "door"]


def test_embedding_failure_records_error_and_falls_back_to_visual_scores() -> None:
    client = MockRealtimeVisionClient()
    embeddings = RecordingEmbeddingProvider(
        {"empty": [1.0, 0.0]},
        failures={
            "changed": {
                "code": "provider_timeout",
                "message": "embedding timed out",
                "recoverable": True,
            }
        },
    )
    app = _app(
        client,
        semantic_detector=SemanticChangeDetector(embeddings, requires_visual_gate=True),
        threshold=0.35,
    )

    app.process_frame(_frame("empty", 0.0, 20, embedding=[1.0, 0.0]))
    result = app.process_frame(_frame("changed", 1.0, 220, embedding=[0.0, 1.0]))

    assert result.metrics.semantic_change_score == 0.0
    assert result.errors[0]["code"] == "provider_timeout"
    assert result.keyframe_selected is True
    assert result.qwen_called is True


def _app(
    client: MockRealtimeVisionClient,
    *,
    threshold: float = 0.35,
    max_interval_seconds: float = 5.0,
    semantic_detector: SemanticChangeDetector | None = None,
) -> RealtimeVideoUnderstandingApp:
    return RealtimeVideoUnderstandingApp(
        qwen_client=client,
        semantic_detector=semantic_detector or SemanticChangeDetector(MetadataEmbeddingModel()),
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


class RecordingEmbeddingProvider:
    def __init__(
        self,
        embeddings: dict[str, list[float]],
        *,
        failures: dict[str, dict[str, object]] | None = None,
    ) -> None:
        self.embeddings = embeddings
        self.failures = failures or {}
        self.calls: list[str] = []

    def embed(self, frame: VideoFrame) -> VisionEmbeddingResult:
        self.calls.append(frame.frame_id)
        if frame.frame_id in self.failures:
            return VisionEmbeddingResult(
                provider="dashscope",
                model="test-embedding",
                errors=[self.failures[frame.frame_id]],
            )
        return VisionEmbeddingResult(
            embedding=self.embeddings.get(frame.frame_id, []),
            provider="dashscope",
            model="test-embedding",
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
