from __future__ import annotations

import asyncio
import json
from pathlib import Path

from assistant_agent.media.embedding.coordinator import SessionEmbeddingCoordinator
from assistant_agent.media.embedding.provider import MockMultimodalEmbeddingProvider
from assistant_agent.media.video.realtime_video_memory import RealtimeVideoMemoryStore
from assistant_agent.media.video.realtime_video_observer import RealtimeVideoObserver
from assistant_agent.media.video.semantic_store import VisualSemanticRecord
from assistant_agent.media.video.semantic_store_pool import (
    SessionVisualSemanticStorePool,
)
from assistant_agent.media.video.video_adapter import FakeRealtimeVisionAdapter
from assistant_agent.media.video.video_context import VideoFrame
from assistant_agent.observability.trace_store import InMemoryTraceStore
from assistant_agent.observability.langfuse_config import local_langfuse_trace_url
from assistant_agent.observability.otel_mapping import build_text_otel_span_specs
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.state import AgentState
from assistant_agent.runtime.tool_executor import ToolExecutor
from assistant_agent.tools.ids import LIVE_VIEW_INSPECT_TOOL_NAME
from assistant_agent.tools.plugins.builtin.media_inspection.tool import (
    LiveViewInspectTool,
    RealtimeVideoObserveTool,
)
from assistant_agent.tools.registry import ToolRegistry


def test_local_langfuse_trace_url_defaults_project_and_rejects_remote_host() -> None:
    trace_id = "b" * 32
    assert local_langfuse_trace_url(
        trace_id,
        {"LANGFUSE_HOST": "http://127.0.0.1:3000"},
    ) == (
        "http://127.0.0.1:3000/project/assistant-agent-local-project/traces/"
        + trace_id
    )
    assert (
        local_langfuse_trace_url(
            trace_id,
            {
                "LANGFUSE_HOST": "https://cloud.langfuse.com",
                "ASSISTANT_AGENT_LANGFUSE_PROJECT_ID": "remote-project",
            },
        )
        is None
    )
    assert local_langfuse_trace_url("not-a-trace-id", {}) is None


def test_background_record_retains_its_own_trace_link(tmp_path: Path) -> None:
    asyncio.run(_assert_background_record_trace_link(tmp_path))


async def _assert_background_record_trace_link(tmp_path: Path) -> None:
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
        user_id="user-vlm-link",
        session_id="session-vlm-link",
        registry=registry,
        memory_store=memory_store,
        embedding_coordinator=SessionEmbeddingCoordinator(
            "session-vlm-link", MockMultimodalEmbeddingProvider()
        ),
        trace_store=trace_store,
        keyframe_root=tmp_path / "keyframes",
    )
    try:
        await observer.promote(
            VideoFrame(
                video_id="video-vlm-link",
                frame_id="frame-vlm-link",
                uri=str(frame_path),
                sequence=7,
                timestamp_ms=700,
            )
        )
        await observer.wait_idle()
        record = observer.semantic_store.latest("video-vlm-link")
    finally:
        await observer.close()

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
    summary = next(
        event
        for event in trace_store.events
        if event.canonical_event == "vision.observation.summary"
    )
    assert record is not None
    assert record.source_vision_trace_id == summary.trace_id
    assert record.source_vision_run_id == summary.run_id
    assert record.source_vlm_span_id == vlm.span_id
    assert vlm.parent_span_id == tool.span_id


def test_live_view_tool_projects_exact_source_trace_link(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("LANGFUSE_HOST", "http://localhost:3000")
    monkeypatch.setenv(
        "ASSISTANT_AGENT_LANGFUSE_PROJECT_ID",
        "assistant-agent-local-project",
    )
    evidence = tmp_path / "source-frame.jpg"
    evidence.write_bytes(b"offline-frame-sentinel")
    pool = SessionVisualSemanticStorePool(root=tmp_path / "semantic-pool")
    semantic_store = pool.resolve("user-live-link", "session-live-link")
    semantic_store.record_success(
        VisualSemanticRecord(
            record_id="visual-record-7",
            session_id="session-live-link",
            video_id="video-live-link",
            frame_sequence=7,
            captured_at_ms=700,
            summary="桌面上有一个蓝色杯子。",
            objects=["杯子"],
            provider="mock",
            model="mock-vlm",
            source_vision_trace_id="a" * 32,
            source_vision_run_id="vision-run-7",
            source_vlm_span_id="vlm-source-span",
            index_status="unavailable",
            evidence_ref=str(evidence),
            evidence_bytes=evidence.stat().st_size,
            created_at_ms=800,
        )
    )
    registry = ToolRegistry()
    registry.register(
        LiveViewInspectTool(
            video_adapter=FakeRealtimeVisionAdapter(),
            memory_store=RealtimeVideoMemoryStore(),
            semantic_store_pool=pool,
        )
    )
    registry.seal()
    trace_store = InMemoryTraceStore()
    state = AgentState.from_request(
        UserRequest(
            user_id="user-live-link",
            session_id="session-live-link",
            text="现在画面里有什么？",
            video_ids=["video-live-link"],
            metadata={
                "transport": "agent_service_websocket",
                "gateway": {"session_config": {"entry_profile": "agent_service"}},
                "agent_service": {"visual_target_sequence": 7},
            },
        )
    )
    try:
        result = ToolExecutor(registry=registry).run_tool(
            state,
            "step-live-link",
            LIVE_VIEW_INSPECT_TOOL_NAME,
            {},
            trace_store=trace_store,
            trace_id=state.trace_id,
        )
    finally:
        pool.close()

    assert result.success is True
    assert result.trace_summary is not None
    assert result.trace_summary["source_vision_trace_id"] == "a" * 32
    specs = build_text_otel_span_specs(trace_store.list_by_run(state.run_id))
    tool = next(
        item
        for item in specs
        if item.attributes.get("gen_ai.tool.name") == LIVE_VIEW_INSPECT_TOOL_NAME
    )
    output = json.loads(tool.attributes["langfuse.observation.output"])
    assert output["source_vision_trace_id"] == "a" * 32
    assert output["source_vision_trace_url"] == (
        "http://localhost:3000/project/assistant-agent-local-project/traces/"
        + "a" * 32
    )
    assert output["source_vlm_span_id"] == "vlm-source-span"
    assert output["source_visual_record_id"] == "visual-record-7"
    assert output["snapshot_sequence"] == 7
    assert tool.attributes[
        "langfuse.observation.metadata.assistant_agent.source_vision_trace_id"
    ] == "a" * 32
    assert tool.attributes[
        "langfuse.observation.metadata.assistant_agent.source_vision_trace_url"
    ] == (
        "http://localhost:3000/project/assistant-agent-local-project/traces/"
        + "a" * 32
    )
    assert "桌面上有一个蓝色杯子。" not in str(tool.attributes)
    assert str(evidence) not in str(tool.attributes)
