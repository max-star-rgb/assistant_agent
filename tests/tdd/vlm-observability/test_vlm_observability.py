from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from assistant_agent.media.embedding.coordinator import SessionEmbeddingCoordinator
from assistant_agent.media.embedding.provider import MockMultimodalEmbeddingProvider
from assistant_agent.media.video.realtime_video_memory import RealtimeVideoMemoryStore
from assistant_agent.media.video.realtime_video_observer import RealtimeVideoObserver
from assistant_agent.media.video.video_adapter import FakeRealtimeVisionAdapter
from assistant_agent.media.video.video_context import VideoFrame
from assistant_agent.media.vision.models import (
    VideoUnderstandingRequest,
    VideoUnderstandingResult,
)
from assistant_agent.media.vision.vision_adapter import MockVisionUnderstandingAdapter
from assistant_agent.media.vision.vision_adapter import VisionUnderstandingInput
from assistant_agent.media.vision.observability import observe_vision_inference
from assistant_agent.observability.otel_exporter import TextOtelTraceObserver
from assistant_agent.observability.otel_mapping import build_text_otel_span_specs
from assistant_agent.observability.trace_conversation import (
    TraceConversationText,
    TraceConversationView,
    TraceToolObservation,
)
from assistant_agent.observability.trace_store import InMemoryTraceStore, TraceEvent
from assistant_agent.runtime.state import AgentState
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.tool_executor import ToolExecutor
from assistant_agent.providers.provider_errors import ProviderAdapterError
from assistant_agent.tools.ids import MEDIA_INSPECT_TOOL_NAME
from assistant_agent.tools.plugins.builtin.media_inspection.tool import MediaInspectTool
from assistant_agent.tools.plugins.builtin.media_inspection.tool import RealtimeVideoObserveTool
from assistant_agent.tools.registry import ToolRegistry
from assistant_agent.tools.base import ToolContext


class _CollectingExporter:
    def __init__(self) -> None:
        self.batches: list[list[object]] = []

    def export(self, spans: list[object]) -> None:
        self.batches.append(spans)


class _ExplodingTraceStore(InMemoryTraceStore):
    def append(self, event: TraceEvent) -> None:
        raise RuntimeError("trace store unavailable")


class _SummaryFailingTraceStore(InMemoryTraceStore):
    def append(self, event: TraceEvent) -> None:
        if event.canonical_event == "vision.observation.summary":
            raise RuntimeError("summary export unavailable")
        super().append(event)


class _FailingRealtimeVisionAdapter:
    def understand_video(
        self, request: VideoUnderstandingRequest
    ) -> VideoUnderstandingResult:
        return VideoUnderstandingResult(
            summary="视觉推理失败。",
            output_ref="mock://vlm/failure",
            errors=[
                {
                    "code": "provider_failed",
                    "message": f"cannot read {request.frame_refs[0]}",
                    "recoverable": True,
                }
            ],
        )


class _SensitiveFailingImageAdapter:
    def understand(self, input: VisionUnderstandingInput) -> object:
        raise ProviderAdapterError(
            "provider_failed",
            "raw-provider-visible-secret-sentinel",
        )


def test_media_tool_emits_vlm_generation_nested_under_tool_span() -> None:
    registry = ToolRegistry()
    registry.register(MediaInspectTool(adapter=MockVisionUnderstandingAdapter()))
    registry.seal()
    store = InMemoryTraceStore()
    state = AgentState.from_request(
        UserRequest(
            user_id="user-vlm",
            session_id="session-vlm",
            text="这张图片里有什么？",
            image_ids=["image-sentinel"],
        )
    )

    result = ToolExecutor(registry=registry).run_tool(
        state,
        "step-vlm",
        MEDIA_INSPECT_TOOL_NAME,
        {},
        trace_store=store,
        trace_id=state.trace_id,
    )

    assert result.success is True
    tool = next(event for event in store.events if event.canonical_event == "tool.finished")
    vlm = next(event for event in store.events if event.canonical_event == "vlm.infer.finished")
    assert vlm.observation_name == "vlm.infer"
    assert vlm.observation_type == "generation"
    assert vlm.parent_span_id == tool.span_id
    assert vlm.provider == "mock"
    assert vlm.attributes["capability"] == "image_understanding"
    assert vlm.attributes["media_kind"] == "image"
    assert vlm.attributes["media_count"] == 1
    assert "image-sentinel" not in str(vlm.model_dump(mode="json"))
    assert result.data["summary"] not in str(vlm.model_dump(mode="json"))


def test_vlm_failure_trace_does_not_expose_provider_error_content() -> None:
    store = InMemoryTraceStore()
    context = ToolContext(
        run_id="run-vlm",
        trace_id="2" * 32,
        trace_store=store,
        parent_span_id="tool-span",
        user_id="user-vlm",
        session_id="session-vlm",
    )

    def fail() -> object:
        raise RuntimeError("failed to read /private/media/frame-secret.jpg")

    with pytest.raises(RuntimeError, match="frame-secret"):
        observe_vision_inference(
            fail,
            context=context,
            capability="image_understanding",
            source="request_image",
            media_kind="image",
            media_count=1,
        )

    event = next(
        item for item in store.events if item.canonical_event == "vlm.infer.finished"
    )
    assert event.error == {
        "code": "provider_call_failed",
        "message": "VLM inference failed.",
    }
    assert "frame-secret" not in str(event.model_dump(mode="json"))


def test_vlm_observability_failure_does_not_change_provider_result() -> None:
    called: list[bool] = []

    result = observe_vision_inference(
        lambda: called.append(True) or object(),
        context=ToolContext(
            run_id="run-vlm",
            trace_id="3" * 32,
            trace_store=_ExplodingTraceStore(),
            parent_span_id="tool-span",
        ),
        capability="image_understanding",
        source="request_image",
        media_kind="image",
        media_count=1,
    )

    assert called == [True]
    assert result is not None


def test_metadata_only_visual_tool_failure_hides_provider_message() -> None:
    registry = ToolRegistry()
    registry.register(MediaInspectTool(adapter=_SensitiveFailingImageAdapter()))
    registry.seal()
    store = InMemoryTraceStore()
    state = AgentState.from_request(
        UserRequest(
            user_id="user-vlm",
            session_id="session-vlm",
            text="inspect",
            image_ids=["image-sentinel"],
        )
    )

    result = ToolExecutor(registry=registry).run_tool(
        state,
        "step-vlm-failed",
        MEDIA_INSPECT_TOOL_NAME,
        {},
        trace_store=store,
        trace_id=state.trace_id,
        failure_mode="continue_to_model",
    )

    assert result.success is False
    failed = next(
        event for event in store.events if event.canonical_event == "tool.failed"
    )
    assert failed.error is not None
    assert failed.error["message"] == "Tool execution failed."
    assert "raw-provider-visible-secret-sentinel" not in str(
        [event.model_dump(mode="json") for event in store.events]
    )


def test_background_vision_batch_uses_independent_trace_identity() -> None:
    events = _background_vision_events()

    specs = build_text_otel_span_specs(events)

    root = specs[0]
    vlm = next(span for span in specs if span.name == "vlm.infer")
    assert root.name == "vision.runtime"
    assert root.parent_span_id is None
    assert root.attributes["langfuse.trace.name"] == "vision.observation"
    assert root.attributes["assistant_agent.modality"] == "vision"
    assert vlm.parent_span_id == "tool-span"
    assert vlm.attributes["langfuse.observation.type"] == "generation"


def test_background_vision_summary_flushes_otel_batch() -> None:
    exporter = _CollectingExporter()
    observer = TextOtelTraceObserver(exporter, enabled=True)

    for event in _background_vision_events():
        observer.on_trace_event(event)

    assert len(exporter.batches) == 1
    assert exporter.batches[0][0].attributes["langfuse.trace.name"] == "vision.observation"


def test_metadata_only_visual_tool_ignores_content_overlay() -> None:
    event = TraceEvent(
        trace_id="4" * 32,
        run_id="run-visual-tool",
        user_id="user-vlm",
        session_id="session-vlm",
        node_name="execute_tool",
        event_type="observability",
        canonical_event="tool.finished",
        observation_type="span",
        span_id="tool-span",
        tool_name=MEDIA_INSPECT_TOOL_NAME,
        status="succeeded",
        input_summary={
            "content_export_policy": "metadata_only",
            "video_ref": "video-secret",
        },
        output_summary={
            "content_export_policy": "metadata_only",
            "model_observation": {"summary": "visual-secret"},
        },
        attributes={"content_export_policy": "metadata_only"},
    )
    conversation = TraceConversationView(
        trace_id=event.trace_id,
        user=TraceConversationText(text="inspect", chars=7),
        assistant=TraceConversationText(text="done", chars=4),
        tool_observations=[
            TraceToolObservation(
                observation_index=1,
                tool_name=MEDIA_INSPECT_TOOL_NAME,
                observation={
                    "tool_name": MEDIA_INSPECT_TOOL_NAME,
                    "summary": "visual-secret",
                    "data": {"media_refs": ["video-secret"]},
                },
            )
        ],
    )

    tool_span = next(
        span
        for span in build_text_otel_span_specs([event], conversation=conversation)
        if span.name == "tool.execute"
    )

    payload = str(tool_span.attributes)
    assert "visual-secret" not in payload
    assert "video-secret" not in payload
    assert "metadata_only" in payload


def test_realtime_observer_records_one_independent_vlm_trace(tmp_path: Path) -> None:
    asyncio.run(_assert_realtime_observer_vlm_trace(tmp_path))


async def _assert_realtime_observer_vlm_trace(tmp_path: Path) -> None:
    frame_path = tmp_path / "frame.jpg"
    frame_path.write_bytes(b"offline-frame-sentinel")
    trace_store = InMemoryTraceStore()
    memory_store = RealtimeVideoMemoryStore()
    registry = ToolRegistry()
    registry.register(
        RealtimeVideoObserveTool(
            video_adapter=FakeRealtimeVisionAdapter(),
            memory_store=memory_store,
        )
    )
    registry.seal()
    observer = RealtimeVideoObserver(
        user_id="user-vlm",
        session_id="session-vlm",
        registry=registry,
        memory_store=memory_store,
        embedding_coordinator=SessionEmbeddingCoordinator(
            "session-vlm", MockMultimodalEmbeddingProvider()
        ),
        trace_store=trace_store,
        keyframe_root=tmp_path / "keyframes",
    )
    try:
        await observer.promote(
            VideoFrame(
                video_id="video-vlm",
                frame_id="frame-vlm",
                uri=str(frame_path),
                sequence=1,
                timestamp_ms=100,
            )
        )
        await observer.wait_idle()
        record = observer.semantic_store.latest("video-vlm")
    finally:
        await observer.close()

    summary = next(
        event
        for event in trace_store.events
        if event.canonical_event == "vision.observation.summary"
    )
    vlm = next(
        event
        for event in trace_store.events
        if event.canonical_event == "vlm.infer.finished"
    )
    tool = next(
        event
        for event in trace_store.events
        if event.canonical_event == "tool.finished"
    )
    assert {summary.run_id, vlm.run_id, tool.run_id} == {summary.run_id}
    assert {summary.trace_id, vlm.trace_id, tool.trace_id} == {summary.trace_id}
    assert vlm.parent_span_id == tool.span_id
    assert summary.attributes["trace_kind"] == "vision_observation"
    assert str(frame_path) not in str(summary.model_dump(mode="json"))
    run_payload = str(
        [
            event.model_dump(mode="json")
            for event in trace_store.list_by_run(summary.run_id)
        ]
    )
    assert record is not None
    assert "video-vlm" not in run_payload
    assert str(frame_path) not in run_payload
    assert record.summary not in run_payload


def test_realtime_observer_failure_summary_hides_media_path(tmp_path: Path) -> None:
    asyncio.run(_assert_realtime_observer_failure_trace(tmp_path))


async def _assert_realtime_observer_failure_trace(tmp_path: Path) -> None:
    frame_path = tmp_path / "frame-secret.jpg"
    frame_path.write_bytes(b"offline-frame-sentinel")
    trace_store = InMemoryTraceStore()
    memory_store = RealtimeVideoMemoryStore()
    registry = ToolRegistry()
    registry.register(
        RealtimeVideoObserveTool(
            video_adapter=_FailingRealtimeVisionAdapter(),
            memory_store=memory_store,
        )
    )
    registry.seal()
    observer = RealtimeVideoObserver(
        user_id="user-vlm",
        session_id="session-vlm",
        registry=registry,
        memory_store=memory_store,
        embedding_coordinator=SessionEmbeddingCoordinator(
            "session-vlm", MockMultimodalEmbeddingProvider()
        ),
        trace_store=trace_store,
        keyframe_root=tmp_path / "keyframes",
    )
    try:
        await observer.promote(
            VideoFrame(
                video_id="video-vlm",
                frame_id="frame-vlm",
                uri=str(frame_path),
                sequence=1,
                timestamp_ms=100,
            )
        )
        await observer.wait_idle()
    finally:
        await observer.close()

    summary = next(
        event
        for event in trace_store.events
        if event.canonical_event == "vision.observation.summary"
    )
    assert summary.error == {
        "code": "provider_failed",
        "message": "VLM observation failed.",
    }
    assert "frame-secret" not in str(summary.model_dump(mode="json"))


def test_realtime_summary_observability_failure_keeps_visual_result(
    tmp_path: Path,
) -> None:
    asyncio.run(_assert_realtime_summary_failure_is_fail_open(tmp_path))


async def _assert_realtime_summary_failure_is_fail_open(tmp_path: Path) -> None:
    frame_path = tmp_path / "frame.jpg"
    frame_path.write_bytes(b"offline-frame-sentinel")
    trace_store = _SummaryFailingTraceStore()
    memory_store = RealtimeVideoMemoryStore()
    registry = ToolRegistry()
    registry.register(
        RealtimeVideoObserveTool(
            video_adapter=FakeRealtimeVisionAdapter(),
            memory_store=memory_store,
        )
    )
    registry.seal()
    observer = RealtimeVideoObserver(
        user_id="user-vlm",
        session_id="session-vlm",
        registry=registry,
        memory_store=memory_store,
        embedding_coordinator=SessionEmbeddingCoordinator(
            "session-vlm", MockMultimodalEmbeddingProvider()
        ),
        trace_store=trace_store,
        keyframe_root=tmp_path / "keyframes",
    )
    try:
        await observer.promote(
            VideoFrame(
                video_id="video-vlm",
                frame_id="frame-vlm",
                uri=str(frame_path),
                sequence=1,
                timestamp_ms=100,
            )
        )
        await observer.wait_idle()
        assert observer.semantic_store.latest("video-vlm") is not None
    finally:
        await observer.close()


def _background_vision_events() -> list[TraceEvent]:
    created_at = datetime(2026, 8, 5, tzinfo=timezone.utc)
    common = {
        "trace_id": "1" * 32,
        "run_id": "vision-run",
        "user_id": "user-vlm",
        "session_id": "session-vlm",
        "node_name": "realtime_video_observer",
        "event_type": "observability",
        "created_at": created_at,
    }
    return [
        TraceEvent(
            **common,
            canonical_event="tool.started",
            span_id="tool-span",
            tool_name="realtime_video_observe",
            status="started",
        ),
        TraceEvent(
            **common,
            canonical_event="vlm.infer.finished",
            observation_name="vlm.infer",
            observation_type="generation",
            span_id="vlm-span",
            parent_span_id="tool-span",
            provider="mock",
            status="succeeded",
            attributes={"model_role": "vlm", "media_kind": "live_view"},
        ),
        TraceEvent(
            **common,
            canonical_event="tool.finished",
            observation_type="span",
            span_id="tool-span",
            tool_name="realtime_video_observe",
            status="succeeded",
        ),
        TraceEvent(
            **common,
            canonical_event="vision.observation.summary",
            status="completed",
            attributes={
                "trace_kind": "vision_observation",
                "media_kind": "live_view",
                "frame_sequence": 7,
            },
            output_summary={"status": "succeeded"},
        ),
    ]
