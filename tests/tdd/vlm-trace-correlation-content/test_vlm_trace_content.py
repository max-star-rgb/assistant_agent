from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any

from assistant_agent.media.vision import observability as vision_observability
from assistant_agent.media.vision.models import VideoUnderstandingResult
from assistant_agent.media.vision.observability import observe_vision_inference
from assistant_agent.observability.otel_mapping import build_text_otel_span_specs
from assistant_agent.observability.trace_conversation import (
    TraceConversationText,
    TraceConversationView,
    TraceVlmOutput,
)
from assistant_agent.observability.trace_store import InMemoryTraceStore, TraceEvent
from assistant_agent.tools.base import ToolContext


class _CollectingVlmContentStore:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def append_vlm_output(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


def test_vlm_result_is_captured_only_in_local_content_overlay(
    monkeypatch,
) -> None:
    monkeypatch.setenv("MULTIMODAL_AGENT_LOCAL_TRACE_CONTENT", "1")
    content_store = _CollectingVlmContentStore()
    monkeypatch.setattr(
        vision_observability,
        "_trace_content_store",
        lambda: content_store,
        raising=False,
    )
    trace_store = InMemoryTraceStore()
    context = ToolContext(
        run_id="vision-run",
        trace_id="1" * 32,
        trace_store=trace_store,
        parent_span_id="tool-span",
        user_id="user-vlm",
        session_id="session-vlm",
    )
    result = VideoUnderstandingResult(
        summary="桌面上有一只杯子。",
        scene="室内桌面",
        objects=["杯子"],
        output_ref="mock://vision/private-output-ref",
        provider="mock",
        model="mock-vlm",
        media_refs=["private-frame-ref"],
    )

    returned = observe_vision_inference(
        lambda: result,
        context=context,
        capability="video_understanding",
        source="background_keyframe_observation",
        media_kind="live_view",
        media_count=1,
    )

    assert returned is result
    assert len(content_store.calls) == 1
    captured = content_store.calls[0]["vlm_output"].normalized_result
    assert captured["summary"] == "桌面上有一只杯子。"
    assert captured["objects"] == ["杯子"]
    assert "output_ref" not in captured
    assert "media_refs" not in captured
    canonical = [event.model_dump(mode="json") for event in trace_store.events]
    assert "桌面上有一只杯子。" not in str(canonical)
    assert "private-output-ref" not in str(canonical)
    assert "private-frame-ref" not in str(canonical)


def test_vlm_content_capture_is_disabled_by_local_policy(monkeypatch) -> None:
    monkeypatch.setenv("MULTIMODAL_AGENT_LOCAL_TRACE_CONTENT", "0")
    content_store = _CollectingVlmContentStore()
    monkeypatch.setattr(
        vision_observability,
        "_trace_content_store",
        lambda: content_store,
    )
    context = ToolContext(
        run_id="vision-run-disabled",
        trace_id="2" * 32,
        trace_store=InMemoryTraceStore(),
        parent_span_id="tool-span",
        user_id="user-vlm",
        session_id="session-vlm",
    )

    observe_vision_inference(
        lambda: VideoUnderstandingResult(
            summary="不应进入 overlay。",
            output_ref="mock://vision/disabled",
        ),
        context=context,
        capability="video_understanding",
        source="background_keyframe_observation",
        media_kind="live_view",
        media_count=1,
    )

    assert content_store.calls == []


def test_vlm_content_overlay_filters_nested_media_refs_and_bounds_text(
    monkeypatch,
) -> None:
    monkeypatch.setenv("MULTIMODAL_AGENT_LOCAL_TRACE_CONTENT", "1")
    content_store = _CollectingVlmContentStore()
    monkeypatch.setattr(
        vision_observability,
        "_trace_content_store",
        lambda: content_store,
    )
    context = ToolContext(
        run_id="vision-run-safe",
        trace_id="5" * 32,
        trace_store=InMemoryTraceStore(),
        parent_span_id="tool-span",
        user_id="user-vlm",
        session_id="session-vlm",
    )

    observe_vision_inference(
        lambda: VideoUnderstandingResult(
            summary="安全边界测试。",
            objects=["x" * 5000],
            timestamps=[
                {
                    "timestamp_ms": 100,
                    "description": "画面开始",
                    "frame_ref": "/private/frame-secret.jpg",
                }
            ],
            output_ref="mock://vision/safe",
        ),
        context=context,
        capability="video_understanding",
        source="background_keyframe_observation",
        media_kind="live_view",
        media_count=1,
    )

    captured = content_store.calls[0]["vlm_output"].normalized_result
    assert len(captured["objects"][0]) <= 4000
    assert captured["timestamps"] == [
        {"timestamp_ms": 100, "description": "画面开始"}
    ]
    assert "frame-secret" not in str(captured)


def test_vlm_inference_reports_its_trace_link_fail_open(monkeypatch) -> None:
    monkeypatch.setenv("MULTIMODAL_AGENT_LOCAL_TRACE_CONTENT", "0")
    links: list[object] = []
    context = ToolContext(
        run_id="vision-run-link",
        trace_id="3" * 32,
        trace_store=InMemoryTraceStore(),
        parent_span_id="tool-span",
        user_id="user-vlm",
        session_id="session-vlm",
    )

    result = observe_vision_inference(
        lambda: VideoUnderstandingResult(
            summary="关联测试。",
            output_ref="mock://vision/link",
        ),
        context=context,
        capability="video_understanding",
        source="background_keyframe_observation",
        media_kind="live_view",
        media_count=1,
        trace_link_callback=links.append,
    )

    assert result.summary == "关联测试。"
    assert len(links) == 1
    assert links[0].trace_id == "3" * 32
    assert links[0].run_id == "vision-run-link"
    assert links[0].span_id

    result = observe_vision_inference(
        lambda: result,
        context=context,
        capability="video_understanding",
        source="background_keyframe_observation",
        media_kind="live_view",
        media_count=1,
        trace_link_callback=lambda _link: (_ for _ in ()).throw(
            RuntimeError("trace callback unavailable")
        ),
    )
    assert result.summary == "关联测试。"


def test_vision_mapping_exports_normalized_vlm_text_from_overlay() -> None:
    events = _background_vision_events()
    conversation = TraceConversationView(
        trace_id="4" * 32,
        user=TraceConversationText(text="", chars=0),
        assistant=TraceConversationText(text="", chars=0),
        vlm_outputs=[
            TraceVlmOutput(
                span_id="vlm-span",
                provider="mock",
                model="mock-vlm",
                normalized_result={
                    "summary": "窗边有一盆绿植。",
                    "scene": "室内窗边",
                    "objects": ["绿植"],
                },
            )
        ],
    )

    specs = build_text_otel_span_specs(events, conversation=conversation)

    root = next(item for item in specs if item.name == "vision.runtime")
    vlm = next(item for item in specs if item.name == "vlm.infer")
    vlm_output = json.loads(vlm.attributes["assistant_agent.observation.output"])
    root_output = json.loads(root.attributes["assistant_agent.trace.output"])
    assert vlm_output["summary"] == "窗边有一盆绿植。"
    assert vlm_output["objects"] == ["绿植"]
    assert root_output["summary"] == "窗边有一盆绿植。"
    assert root_output["scene"] == "室内窗边"


def _background_vision_events() -> list[TraceEvent]:
    created_at = datetime(2026, 8, 6, tzinfo=timezone.utc)
    common = {
        "trace_id": "4" * 32,
        "run_id": "vision-run-mapping",
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
            model="mock-vlm",
            status="succeeded",
            output_summary={"status": "succeeded"},
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
