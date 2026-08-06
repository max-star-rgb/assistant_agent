from __future__ import annotations

from datetime import datetime, timezone
import json
from types import SimpleNamespace
from typing import Any

from assistant_agent.media.vision import observability as vision_observability
from assistant_agent.media.vision.models import VideoUnderstandingResult
from assistant_agent.media.vision.models import (
    VideoUnderstandingRequest,
    VisionUnderstandingRequest,
)
from assistant_agent.media.vision.observability import observe_vision_inference
from assistant_agent.observability.otel_exporter import OtlpHttpTextExporterConfig
from assistant_agent.observability.otel_mapping import build_text_otel_span_specs
from assistant_agent.observability.trace_conversation import (
    InMemoryTraceConversationStore,
    TraceConversationText,
    TraceConversationView,
)
from assistant_agent.observability.trace_store import TraceEvent
from assistant_agent.observability.trace_store import InMemoryTraceStore
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.plugins.builtin.media_inspection.tool import (
    RealtimeVideoObserveTool,
)


class _CollectingVlmStore:
    def __init__(self) -> None:
        self.inputs: list[dict[str, Any]] = []

    def append_vlm_input(self, **kwargs: Any) -> None:
        self.inputs.append(kwargs)

    def append_vlm_output(self, **_kwargs: Any) -> None:
        return


class _TraceableVideoAdapter:
    provider = "traceable-vlm"

    def resolved_instructions(self, request: VideoUnderstandingRequest) -> str:
        return f"resolved-for:{request.user_query}"

    def understand_video(
        self,
        request: VideoUnderstandingRequest,
    ) -> VideoUnderstandingResult:
        return VideoUnderstandingResult(
            summary="traceable-result",
            provider=self.provider,
            model="traceable-model",
            output_ref="traceable://result",
        )


class _BrokenTraceInputVideoAdapter(_TraceableVideoAdapter):
    def resolved_instructions(self, request: VideoUnderstandingRequest) -> str:
        raise RuntimeError("trace input unavailable")


def _vision_events() -> list[TraceEvent]:
    common = {
        "trace_id": "6" * 32,
        "run_id": "vision-run-input",
        "user_id": "user-vlm",
        "session_id": "session-vlm",
        "node_name": "vision_understanding",
        "event_type": "observability",
        "created_at": datetime(2026, 8, 6, tzinfo=timezone.utc),
    }
    return [
        TraceEvent(
            **common,
            canonical_event="vlm.infer.finished",
            observation_name="vlm.infer",
            observation_type="generation",
            span_id="vlm-input-span",
            parent_span_id="tool-span",
            status="succeeded",
            attributes={
                "media_kind": "live_view",
                "source": "background_keyframe_observation",
                "frame_sequence": 17,
            },
        ),
        TraceEvent(
            **common,
            canonical_event="vision.observation.summary",
            status="completed",
            attributes={
                "trace_kind": "vision_observation",
                "media_kind": "live_view",
                "frame_sequence": 17,
            },
        ),
    ]


def test_vlm_generation_uses_local_input_overlay_for_prompt_and_context() -> None:
    conversation = TraceConversationView(
        trace_id="6" * 32,
        user=TraceConversationText(text="", chars=0),
        assistant=TraceConversationText(text="", chars=0),
    ).model_copy(
        update={
            "vlm_inputs": [
                SimpleNamespace(
                    span_id="vlm-input-span",
                    normalized_input={
                        "mode": "background_keyframe_observation",
                        "prompt_version": "realtime-single-frame-v1",
                        "resolved_instructions": "role-sentinel\nquestion-sentinel",
                        "query": "question-sentinel",
                        "media_kind": "live_view",
                        "frame_sequence": 17,
                        "frame_count": 1,
                        "history_frame_count": 0,
                        "memory_context_present": False,
                    },
                )
            ]
        }
    )

    specs = build_text_otel_span_specs(_vision_events(), conversation=conversation)

    root = next(item for item in specs if item.name == "vision.runtime")
    vlm = next(item for item in specs if item.name == "vlm.infer")
    expected_input = {
        "mode": "background_keyframe_observation",
        "prompt_version": "realtime-single-frame-v1",
        "resolved_instructions": "role-sentinel\nquestion-sentinel",
        "query": "question-sentinel",
        "media_kind": "live_view",
        "frame_sequence": 17,
        "frame_count": 1,
        "history_frame_count": 0,
        "memory_context_present": False,
        "content_exported": True,
    }
    assert json.loads(vlm.attributes["langfuse.observation.input"]) == expected_input
    assert json.loads(root.attributes["langfuse.trace.input"]) == expected_input


def test_vlm_input_content_export_is_enabled_only_for_loopback() -> None:
    local = OtlpHttpTextExporterConfig.from_env(
        {
            "ASSISTANT_AGENT_OTEL_EXPORT_ENABLED": "true",
            "ASSISTANT_AGENT_OTEL_EXPORT_ENDPOINT": (
                "http://127.0.0.1:3000/api/public/otel/v1/traces"
            ),
        }
    )
    remote = OtlpHttpTextExporterConfig.from_env(
        {
            "ASSISTANT_AGENT_OTEL_EXPORT_ENABLED": "true",
            "ASSISTANT_AGENT_OTEL_EXPORT_ENDPOINT": (
                "https://langfuse.example.com/api/public/otel/v1/traces"
            ),
        }
    )

    assert local.include_vlm_input_content is True
    assert remote.include_vlm_input_content is False


def test_vlm_input_overlay_captures_safe_prompt_without_canonical_content(
    monkeypatch,
) -> None:
    monkeypatch.setenv("MULTIMODAL_AGENT_LOCAL_TRACE_CONTENT", "1")
    content_store = _CollectingVlmStore()
    monkeypatch.setattr(
        vision_observability,
        "_trace_content_store",
        lambda: content_store,
    )
    trace_store = InMemoryTraceStore()
    context = ToolContext(
        run_id="vision-run-input",
        trace_id="7" * 32,
        trace_store=trace_store,
        parent_span_id="tool-span",
        user_id="user-vlm",
        session_id="session-vlm",
    )

    observe_vision_inference(
        lambda: VideoUnderstandingResult(
            summary="result-sentinel",
            output_ref="mock://vlm/input-overlay",
        ),
        context=context,
        capability="video_understanding",
        source="background_keyframe_observation",
        media_kind="live_view",
        media_count=1,
        local_input_content={
            "mode": "background_keyframe_observation",
            "prompt_version": "realtime-single-frame-v1",
            "resolved_instructions": "prompt-sentinel",
            "query": "query-sentinel",
            "media_kind": "live_view",
            "frame_sequence": 21,
            "frame_count": 1,
            "history_frame_count": 0,
            "memory_context_present": False,
            "frame_ref": "/private/frame-secret.jpg",
        },
    )

    assert len(content_store.inputs) == 1
    captured = content_store.inputs[0]["vlm_input"].normalized_input
    assert captured == {
        "mode": "background_keyframe_observation",
        "prompt_version": "realtime-single-frame-v1",
        "resolved_instructions": "prompt-sentinel",
        "query": "query-sentinel",
        "media_kind": "live_view",
        "frame_sequence": 21,
        "frame_count": 1,
        "history_frame_count": 0,
        "memory_context_present": False,
    }
    assert "prompt-sentinel" not in str(
        [event.model_dump(mode="json") for event in trace_store.events]
    )
    assert "query-sentinel" not in str(
        [event.model_dump(mode="json") for event in trace_store.events]
    )


def test_realtime_video_tool_records_adapter_resolved_prompt_and_empty_history(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("MULTIMODAL_AGENT_LOCAL_TRACE_CONTENT", "1")
    content_store = InMemoryTraceConversationStore()
    monkeypatch.setattr(
        vision_observability,
        "_trace_content_store",
        lambda: content_store,
    )
    frame = tmp_path / "private-frame.jpg"
    frame.write_bytes(b"offline-frame")
    trace_store = InMemoryTraceStore()
    context = ToolContext(
        run_id="vision-run-tool-input",
        trace_id="8" * 32,
        trace_store=trace_store,
        parent_span_id="tool-span",
        user_id="user-vlm",
        session_id="session-vlm",
        metadata={"realtime_video_observation": True},
    )

    result = RealtimeVideoObserveTool(video_adapter=_TraceableVideoAdapter()).run(
        VisionUnderstandingRequest(
            video_ref="video-private",
            frame_refs=[str(frame)],
            user_query="query-from-tool",
            metadata={"frame_sequence": 23},
        ),
        context,
    )

    assert result.success is True
    view = content_store.get(
        user_id="user-vlm",
        session_id="session-vlm",
        trace_id="8" * 32,
        include_vlm_inputs=True,
    )
    assert view is not None
    assert view.vlm_inputs[0].normalized_input == {
        "mode": "background_keyframe_observation",
        "prompt_version": "realtime-single-frame-v1",
        "resolved_instructions": "resolved-for:query-from-tool",
        "query": "query-from-tool",
        "media_kind": "live_view",
        "frame_sequence": 23,
        "frame_count": 1,
        "history_frame_count": 0,
        "memory_context_present": False,
    }
    assert str(frame) not in str(view.vlm_inputs[0].normalized_input)


def test_vlm_input_capture_failure_does_not_fail_visual_inference(tmp_path) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"offline-frame")

    result = RealtimeVideoObserveTool(
        video_adapter=_BrokenTraceInputVideoAdapter()
    ).run(
        VisionUnderstandingRequest(
            video_ref="video-safe",
            frame_refs=[str(frame)],
            user_query="query-safe",
            metadata={"frame_sequence": 24},
        ),
        ToolContext(metadata={"realtime_video_observation": True}),
    )

    assert result.success is True
    assert result.data is not None
    assert result.data["summary"] == "traceable-result"
